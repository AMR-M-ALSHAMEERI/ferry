"""The Antigravity adapter: detect, export and path-remapped import.

Antigravity keeps one SQLite database per conversation and stores the content
of those conversations as protobuf blobs inside them. Ferry has no schema for
those blobs, and that single fact decides the shape of this adapter:

* **Export** builds UCS messages for reading, and carries the original
  database into the bundle as ``source_raw`` for restoring.
* **Import** restores from the database, rewriting the absolute paths inside
  it. It does not rebuild one from UCS, because rebuilding would mean
  inventing the parts Ferry could not read.

A conversation from another tool therefore cannot be written *into*
Antigravity yet -- it has no database to restore -- and is reported as skipped
rather than half-written. That is cross-tool migration, which PLAN.md puts at
M7b.
"""

from __future__ import annotations

import json
import platform
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID

from ferry import __version__
from ferry.adapters.antigravity import paths as ag_paths
from ferry.adapters.antigravity import schema, wire
from ferry.adapters.antigravity.build import BUILD_NOTES, build_database, model_identifier
from ferry.adapters.antigravity.index import (
    AntigravityIndexLocked,
    drop_entry,
    entry_for,
    index_path,
    upsert_entry,
)
from ferry.adapters.antigravity.reader import (
    SessionRead,
    parent_conversation,
    read_conversation,
    step_text,
)
from ferry.adapters.antigravity.remap import PathRemapper
from ferry.adapters.antigravity.writer import (
    antigravity_is_running,
    remap_database,
    snapshot,
    write_project,
)
from ferry.adapters.base import (
    Adapter,
    DetectResult,
    ExportEvent,
    ImportEvent,
    ImportOptions,
    RemovalBlocked,
)
from ferry.adapters.census import census, count_of
from ferry.adapters.conflict import RENAME_NOT_POSSIBLE
from ferry.adapters.dedup import compare_duplicate
from ferry.adapters.formatcheck import FormatCheck
from ferry.core import Bundle, Manifest, SourceMachine, back_up
from ferry.core import provenance as provenance_store
from ferry.core.compat import assess, refusal
from ferry.core.manifest import OSName
from ferry.ucs import Conversation, Provenance, ToolName

__all__ = ["TESTED_VERSION", "TOOL", "AntigravityAdapter"]

SQLITE_MAGIC: Final = b"SQLite format 3\x00"
"""The first sixteen bytes of every SQLite file.

Here because a conversation is restored by copying a database into place, and
"there is a file in the bundle" is not the same question as "that file is a
database". The M7b probe found the difference the expensive way: handed a
Claude Code conversation, this adapter copied its 1.6 MB **JSONL transcript**
to ``conversations/<uuid>.db``, remapped it without complaint, and reported
``1 of 1 imported``. Nothing was there afterwards. A success message over an
empty result is worse than an error, because nobody goes looking.
"""


TOOL: Final[ToolName] = "antigravity"

TESTED_VERSION: Final = "2.8.1"
"""The Antigravity release this adapter was built and verified against."""

PROJECT_SIDECAR = "project.json"
"""Where a conversation's project record is carried inside the bundle."""

_OS_NAMES: dict[str, OSName] = {"Windows": "win32", "Darwin": "darwin", "Linux": "linux"}


def _trajectories(count: int) -> str:
    return count_of(count, "subagent trajectory", "subagent trajectories")


def _os_name() -> OSName:
    return _OS_NAMES.get(platform.system(), "linux")


SAMPLE_STEPS: Final = 60
"""How many steps the format check reads before deciding.

Enough to meet several step types in a real conversation, small enough that the
check costs milliseconds -- it runs on every scan.
"""


def check_format(database: Path, env: dict[str, str] | None = None) -> FormatCheck:
    """Whether Antigravity still stores conversations the way this adapter reads them.

    Three things are checked, in the order they would break:

    1. The blobs are protobuf this codec can parse.
    2. The step types are ones with names (28 excepted -- it has always been
       there and has never been in the transcript).
    3. The steps that should carry text actually do.

    The third is the one worth having. A renumbered field is the failure this
    format is prone to and the only one that is otherwise silent: everything
    would look successful and the conversations would come out empty.
    """
    findings: list[str] = []
    try:
        with ag_paths.open_readonly(database) as connection:
            rows = connection.execute(
                "SELECT step_type, step_payload FROM steps ORDER BY idx DESC LIMIT ?",
                (SAMPLE_STEPS,),
            ).fetchall()
    except Exception:  # noqa: BLE001 - an unreadable sample is reported, never raised
        return FormatCheck(checked=False)

    if not rows:
        return FormatCheck(checked=False)

    parsed = total = named = with_text = expected_text = 0
    for step_type, payload in rows:
        if not isinstance(payload, bytes | bytearray) or not payload:
            continue
        total += 1
        if wire.parse(bytes(payload)) is not None:
            parsed += 1
        if int(step_type) in schema.STEP_TYPES:
            named += 1
        if int(step_type) in schema.TEXT_FIELDS and int(step_type) != schema.CHECKPOINT:
            expected_text += 1
            if step_text(bytes(payload), int(step_type)):
                with_text += 1

    if not total:
        return FormatCheck(checked=False)

    if parsed < total:
        findings.append(f"{total - parsed} of {total} sampled steps are not readable protobuf")
    if named == 0:
        findings.append(f"none of {total} sampled steps has a step type Ferry recognises")
    # A majority failing is a renumbered field. One or two is a step that
    # genuinely held nothing, which is ordinary.
    if expected_text and with_text * 2 < expected_text:
        findings.append(
            f"no message text was found in {expected_text - with_text} of {expected_text} "
            "steps that should carry some"
        )

    return FormatCheck(checked=True, findings=findings)


@dataclass
class _Tally:
    """What the whole export has to say, gathered so it can be said once.

    Each of these is a fact about the storage format rather than about one
    conversation, so reporting it per conversation produced four copies of the
    same sentence with different numbers -- and, because notes were emitted as
    warnings, four warning markers for something entirely routine.
    """

    images: int = 0
    conversations_with_images: int = 0
    checkpoints: int = 0
    unknown_types: set[int] = field(default_factory=set)
    conversations_with_unknown: int = 0
    subagents: int = 0

    def add(self, found: SessionRead) -> None:
        if found.attachments:
            self.images += len(found.attachments)
            self.conversations_with_images += 1
        self.checkpoints += found.checkpoints
        if found.unknown_types:
            self.unknown_types.update(found.unknown_types)
            self.conversations_with_unknown += 1
        if found.parent_id:
            self.subagents += 1

    def events(self) -> Iterator[ExportEvent]:
        """The notes and warnings for this export, each stated once."""
        if self.images:
            yield ExportEvent(
                kind="note",
                message=(
                    f"{count_of(self.images, 'image was', 'images were')} attached across "
                    f"{count_of(self.conversations_with_images, 'conversation')}. Antigravity "
                    "does not record which message an upload belonged to, so they are "
                    "attached to the conversation rather than placed in it."
                ),
            )
        if self.checkpoints:
            yield ExportEvent(
                kind="note",
                message=(
                    f"{count_of(self.checkpoints, 'checkpoint step')} held snapshots of your "
                    "files rather than conversation, and are not shown as messages. The "
                    "original databases in the bundle still carry them."
                ),
            )
        if self.unknown_types:
            listed = ", ".join(str(value) for value in sorted(self.unknown_types))
            kinds = "step type" if len(self.unknown_types) == 1 else "step types"
            yield ExportEvent(
                kind="warning",
                message=(
                    f"{count_of(self.conversations_with_unknown, 'conversation')} contain "
                    f"{kinds} {listed}, which Ferry has no name for. Those steps were read "
                    "as assistant messages."
                ),
            )


def _is_database(path: Path) -> bool:
    """Whether ``path`` really is a SQLite database.

    The header, not the extension: the extension is chosen by whoever wrote the
    file and the header is chosen by SQLite. Any read error counts as "no" --
    a file that cannot be read cannot be verified, and installing it unchecked
    is the thing this exists to stop.
    """
    try:
        with path.open("rb") as handle:
            return handle.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


class AntigravityAdapter(Adapter):
    """Ferry's reader and writer for Antigravity conversation history."""

    name = TOOL
    display_name = "Antigravity"

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self._env = env

    # ---------- detect ----------

    def detect(self) -> DetectResult:
        try:
            root = ag_paths.data_dir(self._env)
        except (OSError, RuntimeError) as exc:
            return DetectResult(
                installed=False, notes=[f"could not resolve Antigravity data: {exc}"]
            )

        if not root.is_dir():
            return DetectResult(installed=False, notes=[f"no Antigravity data directory at {root}"])

        try:
            databases = ag_paths.conversation_databases(self._env)
        except OSError as exc:
            return DetectResult(installed=False, notes=[f"cannot read {root}: {exc}"])

        if not databases:
            return DetectResult(
                installed=False,
                data_paths=[root],
                notes=[f"{root} exists but holds no conversations yet"],
            )

        total = 0
        for database in databases:
            for suffix in ("", "-wal"):
                sidecar = database.with_name(database.name + suffix)
                try:
                    total += sidecar.stat().st_size
                except OSError:
                    continue

        # Count what Antigravity shows, not what is on disk. A subagent
        # trajectory has its own database and is not a conversation the user
        # has -- reporting six where the app lists two is the kind of wrong
        # that makes every other number the tool prints untrustworthy.
        counted = census(
            [(database.stem, database) for database in databases],
            lambda _id, _path: True,
            hidden=lambda _id, path: parent_conversation(path, self._env) is not None,
            hidden_label="",
        )

        notes = [f"{total / 1024 / 1024:.1f} MB of conversation databases at {root}"]
        if counted.hidden:
            notes.append(
                f"{_trajectories(counted.hidden)}, carried with the conversations that spawned them"
            )
        projects = ag_paths.projects(self._env)
        folders = sum(1 for project in projects.values() if project.folder)
        if folders:
            notes.append(f"{folders} project folders")

        version = ag_paths.antigravity_version(self._env)

        # Checked against the newest conversation, not against the version
        # number. A format change appears in new data first -- files written by
        # the old version keep parsing perfectly.
        newest = max(databases, key=lambda path: path.stat().st_mtime)
        checked = check_format(newest, self._env)

        return DetectResult(
            installed=True,
            version=version,
            data_paths=[root],
            conversation_count_estimate=counted.conversations,
            notes=notes,
            caveats=checked.caveats(self.display_name, version),
        )

    # ---------- export ----------

    def _open_bundle(self, dest_bundle_dir: Path) -> Bundle:
        """Open the destination bundle, creating it if this is a fresh export."""
        if (dest_bundle_dir / "manifest.json").is_file():
            return Bundle.open(dest_bundle_dir)
        return Bundle.create(dest_bundle_dir, self._manifest())

    def _manifest(self) -> Manifest:
        return Manifest(
            created_at=datetime.now(UTC),
            created_by=f"ferry {__version__}",
            source_machine=SourceMachine(
                os=_os_name(),
                hostname=platform.node() or "unknown",
                user_home=str(Path.home()),
            ),
            tools_included=[TOOL],
        )

    def export(self, dest_bundle_dir: Path) -> Iterator[ExportEvent]:
        bundle = self._open_bundle(dest_bundle_dir)
        databases = ag_paths.conversation_databases(self._env)
        if not databases:
            yield ExportEvent(
                kind="error",
                message=f"no conversations under {ag_paths.conversations_dir(self._env)}",
            )
            return

        parents = {database: parent_conversation(database, self._env) for database in databases}
        top_level = [database for database in databases if not parents[database]]

        yield ExportEvent(
            kind="started",
            message=f"{len(top_level)} conversations to read",
            total=len(databases),
        )

        exported = subagents = 0
        tally = _Tally()
        for database in databases:
            for event in self._export_one(bundle, database, parents[database], tally):
                if event.kind == "progress":
                    if parents[database]:
                        subagents += 1
                    else:
                        exported += 1
                yield event

        yield from tally.events()

        # Both numbers, always. The conversation count is the one the user can
        # check against the app; the subagent count explains the extra files in
        # the bundle before they notice them and wonder.
        detail = f"{exported} of {len(top_level)} conversations exported"
        if subagents:
            detail += f", plus {_trajectories(subagents)} they spawned"
        yield ExportEvent(kind="done", message=detail)

    def _skip_duplicate(
        self, bundle: Bundle, database: Path, conversation_id: UUID
    ) -> Iterator[ExportEvent]:
        """Skip a conversation already in the bundle -- after checking it.

        Shared with the other three adapters: skipping by id alone is what
        makes an interrupted export resumable, and is right only while two
        copies carrying one id agree. See :mod:`ferry.adapters.dedup`.
        """
        try:
            incoming = read_conversation(database, self._env).conversation
            existing = bundle.load_conversation(conversation_id)
        except (OSError, ValueError):
            incoming = existing = None

        verdict = compare_duplicate(conversation_id, incoming, existing, database.name)
        if verdict.warning:
            yield ExportEvent(
                kind="warning", conversation_id=str(conversation_id), message=verdict.warning
            )
        yield ExportEvent(
            kind="skipped", conversation_id=str(conversation_id), message=verdict.message
        )

    def _export_one(
        self,
        bundle: Bundle,
        database: Path,
        parent: str | None = None,
        tally: _Tally | None = None,
    ) -> Iterator[ExportEvent]:
        conversation_id = ag_paths.conversation_id_of(database)
        if conversation_id is None:
            yield ExportEvent(kind="skipped", message=f"{database.name}: not a conversation file")
            return
        if bundle.has_conversation(conversation_id):
            yield from self._skip_duplicate(bundle, database, conversation_id)
            return

        found = read_conversation(database, self._env)
        for warning in found.warnings:
            yield ExportEvent(kind="warning", conversation_id=str(conversation_id), message=warning)

        if found.conversation is None:
            yield ExportEvent(
                kind="skipped",
                conversation_id=str(conversation_id),
                message=found.warnings[0] if found.warnings else "no messages",
            )
            return

        # A conversation Ferry built here looks native on the way back out --
        # that is the whole reason the record is kept in Ferry's own store
        # rather than in Antigravity's file. Reading it back is what carries the
        # origin across an export, and the other three adapters have always done
        # it; this one did not, which made `docs/CROSS-TOOL.md` untrue of it.
        found.conversation.provenance = provenance_store.recall(TOOL, conversation_id)

        for pending in found.attachments:
            bundle.add_attachment(conversation_id, pending.source, pending.record)
        bundle.add_conversation(found.conversation)

        # The database goes in whole, through SQLite rather than as a file
        # copy, so the WAL comes with it. This is what an import restores from.
        with tempfile.TemporaryDirectory(prefix="ferry-antigravity-") as scratch:
            copy = Path(scratch) / database.name
            try:
                snapshot(database, copy)
            except Exception as exc:  # noqa: BLE001 - reported, never raised at the CLI
                yield ExportEvent(
                    kind="warning",
                    conversation_id=str(conversation_id),
                    message=(
                        f"could not copy the original database ({exc}); this conversation is "
                        "readable in the bundle but cannot be imported back into Antigravity"
                    ),
                )
            else:
                bundle.add_source_raw(conversation_id, copy)

        self._carry_project(bundle, conversation_id, found.project_id)

        detail = f"{len(found.conversation.messages)} messages"
        if found.attachments:
            detail += f", {len(found.attachments)} attachments"
        if parent:
            detail = f"subagent of {parent[:8]}: {detail}"
        yield ExportEvent(kind="progress", conversation_id=str(conversation_id), message=detail)

        if tally is not None:
            tally.add(found)

    def _carry_project(self, bundle: Bundle, conversation_id: UUID, project_id: str | None) -> None:
        """Put the conversation's project record in the bundle beside it.

        Small, and the import cannot file the conversation without it: a
        conversation names a project by id, and an id naming nothing is a
        conversation Antigravity has nowhere to show.
        """
        if not project_id:
            return
        source = ag_paths.projects_dir(self._env) / f"{project_id}.json"
        if source.is_file():
            bundle.add_source_raw_sidecar(conversation_id, source, PROJECT_SIDECAR)

    # ---------- remove ----------

    def written_roots(self) -> list[Path]:
        return [ag_paths.conversations_dir(self._env)]

    def listing(self, written: Path) -> Path | None:
        return index_path(self._env)

    def unlist(self, written: Path) -> None:
        conversation_id = ag_paths.conversation_id_of(written)
        if conversation_id is None:
            return
        try:
            drop_entry(index_path(self._env), conversation_id)
        except AntigravityIndexLocked as exc:
            raise RemovalBlocked(str(exc)) from exc

    def in_use(self) -> str | None:
        if antigravity_is_running(self._env):
            return (
                "Antigravity is running. It keeps its conversation list in memory and "
                "writes it back when it closes, so close it and try again."
            )
        return None

    def used_since(self, written: Path) -> bool:
        """Whether SQLite is holding work for this database in its journal.

        A conversation carried on in Antigravity can sit in ``-wal`` while the
        main file stays byte for byte as Ferry wrote it, so the fingerprint
        alone would call it untouched.
        """
        journal = written.with_name(written.name + "-wal")
        try:
            return journal.stat().st_size > 0
        except OSError:
            return False

    def companions(self, written: Path) -> list[Path]:
        return [written.with_name(written.name + suffix) for suffix in ("-wal", "-shm")]

    # ---------- import ----------

    def import_(self, bundle_dir: Path, options: ImportOptions) -> Iterator[ImportEvent]:
        try:
            bundle = Bundle.open(bundle_dir)
        except Exception as exc:  # noqa: BLE001 - reported, never raised at the CLI
            yield ImportEvent(kind="error", message=f"cannot open bundle: {exc}")
            return

        conversations = bundle.list_conversations()
        if not conversations:
            yield ImportEvent(kind="error", message="bundle holds no conversations")
            return

        if antigravity_is_running(self._env):
            yield ImportEvent(
                kind="warning",
                message=(
                    "Antigravity appears to be running. It keeps conversation databases "
                    "open, so close it before importing."
                ),
            )

        remapper = PathRemapper(options.path_remap)
        yield ImportEvent(kind="started", message=f"{len(conversations)} conversations to write")

        written = 0
        for conversation_id in conversations:
            if options.only and str(conversation_id) not in options.only:
                continue
            for event in self._import_one(bundle, conversation_id, options, remapper):
                if event.kind == "progress":
                    written += 1
                yield event
        yield ImportEvent(kind="done", message=f"{written} of {len(conversations)} imported")

    def _destination(self, conversation_id: UUID) -> Path:
        return ag_paths.conversations_dir(self._env) / f"{conversation_id}.db"

    def _import_one(
        self,
        bundle: Bundle,
        conversation_id: UUID,
        options: ImportOptions,
        remapper: PathRemapper,
    ) -> Iterator[ImportEvent]:
        identifier = str(conversation_id)
        try:
            conversation = bundle.load_conversation(conversation_id)
        except Exception as exc:  # noqa: BLE001 - reported, never raised at the CLI
            yield ImportEvent(kind="error", conversation_id=identifier, message=str(exc))
            return

        if conversation.source_tool != TOOL:
            if not options.allow_cross_tool:
                yield ImportEvent(
                    kind="skipped",
                    conversation_id=identifier,
                    message=f"came from {conversation.source_tool}, not Antigravity",
                )
                return
            why = refusal(conversation.source_tool, TOOL)
            if why:
                # Asked for and still refused. The flag says the person accepts
                # a lossy conversion; it does not make an impossible one work,
                # and answering it with a half-written file would be obeying the
                # instruction rather than the intent.
                yield ImportEvent(kind="skipped", conversation_id=identifier, message=why)
                return
            # A conversation from another tool has no original database, so it
            # is built rather than restored. Everything below this point is the
            # restore path and needs one.
            yield from self._build_one(conversation, options)
            return

        original = bundle.source_raw_path(conversation_id)
        if not original.is_file():
            # Either a conversation from another tool, or one whose database
            # could not be copied at export. Both are the same situation here:
            # there is nothing to restore, and writing a partial conversation
            # would be worse than writing none.
            yield ImportEvent(
                kind="skipped",
                conversation_id=identifier,
                message=(
                    "no original database in the bundle; Antigravity conversations are "
                    "restored from it, so this one cannot be written"
                ),
            )
            return

        if not _is_database(original):
            # Present, and not a database. Every tool writes *something* to
            # source_raw, so the file being there says nothing about what it is
            # -- see SQLITE_MAGIC. Checked for native conversations too: a
            # truncated or half-copied database fails here rather than being
            # installed as a conversation nobody can open.
            yield ImportEvent(
                kind="skipped",
                conversation_id=identifier,
                message=(
                    "the bundle's original file is not a SQLite database, so it is not "
                    "an Antigravity conversation and was not written"
                ),
            )
            return

        destination = self._destination(conversation_id)
        if destination.exists():
            if options.on_conflict == "skip":
                yield ImportEvent(
                    kind="skipped",
                    conversation_id=identifier,
                    message="already in Antigravity",
                )
                return
            if options.on_conflict == "rename":
                yield ImportEvent(
                    kind="skipped",
                    conversation_id=identifier,
                    message=RENAME_NOT_POSSIBLE,
                )
                return

        if options.dry_run:
            yield ImportEvent(
                kind="progress",
                conversation_id=identifier,
                message=f"would write {destination.name}",
            )
            return

        try:
            written = self._write(bundle, conversation_id, original, destination, options, remapper)
        except Exception as exc:  # noqa: BLE001 - reported, never raised at the CLI
            yield ImportEvent(kind="error", conversation_id=identifier, message=str(exc))
            return

        yield ImportEvent(kind="progress", conversation_id=identifier, message=written)

    def _project_for(self, conversation: Conversation) -> str | None:
        """Which project a built conversation belongs to.

        Antigravity groups conversations by project, and a conversation with no
        project is one the list has nowhere to put. Preferred is the project
        whose folder is the one this conversation happened in; failing that, any
        project with a folder, so it lands somewhere a person will look.
        """
        found = ag_paths.projects(self._env)
        if not found:
            return None
        workspace = (conversation.workspace.original_path or "").replace("\\", "/").lower()
        if workspace:
            for project in found.values():
                folder = (project.folder or "").replace("\\", "/").lower()
                if folder and (folder.endswith(workspace) or workspace.endswith(folder)):
                    return project.id
        with_folder = [p for p in found.values() if p.folder]
        return (with_folder or list(found.values()))[0].id

    def _identifier(self) -> bytes:
        """The model identifier, copied from a conversation that already has one."""
        for database in ag_paths.conversation_databases(self._env):
            found = model_identifier(database)
            if found:
                return found
        return b""

    def _build_one(
        self, conversation: Conversation, options: ImportOptions
    ) -> Iterator[ImportEvent]:
        """Write a conversation from another tool as an Antigravity conversation.

        Two stores, and the second one is the whole reason this works: the
        database holds the conversation, and the entry in
        ``agyhub_summaries_proto.pb`` is what makes Antigravity know it exists.
        A database with no entry is a conversation nothing lists -- measured by
        cloning one Antigravity *does* list and watching it not appear.
        """
        identifier = str(conversation.id)
        destination = self._destination(conversation.id)

        if destination.exists():
            if options.on_conflict == "skip":
                yield ImportEvent(
                    kind="skipped", conversation_id=identifier, message="already in Antigravity"
                )
                return
            if options.on_conflict == "rename":
                yield ImportEvent(
                    kind="skipped", conversation_id=identifier, message=RENAME_NOT_POSSIBLE
                )
                return

        project_id = self._project_for(conversation)
        if project_id is None:
            yield ImportEvent(
                kind="skipped",
                conversation_id=identifier,
                message=(
                    "Antigravity has no projects on this machine yet, and a conversation "
                    "belongs to one; open a folder in Antigravity once and import again"
                ),
            )
            return

        if options.dry_run:
            yield ImportEvent(
                kind="progress",
                conversation_id=identifier,
                message=f"would build {destination.name} and list it in {index_path().name}",
            )
            return

        if options.backup and destination.exists():
            back_up(destination, TOOL)

        staged = destination.with_name(destination.name + ".ferry-tmp")
        if staged.exists():
            staged.unlink()
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            steps = build_database(
                staged,
                conversation,
                project_id=project_id,
                identifier=self._identifier(),
            )
            staged.replace(destination)
        except Exception as exc:  # noqa: BLE001 - reported, never raised at the CLI
            if staged.exists():
                staged.unlink()
            yield ImportEvent(kind="error", conversation_id=identifier, message=str(exc))
            return

        for suffix in ("-wal", "-shm"):
            stale = destination.with_name(destination.name + suffix)
            if stale.exists():
                stale.unlink()

        created = conversation.created_at or conversation.updated_at
        updated = conversation.updated_at or created
        try:
            upsert_entry(
                index_path(self._env),
                conversation.id,
                entry_for(
                    conversation.id,
                    title=conversation.title or "Imported conversation",
                    project_id=project_id,
                    identifier=self._identifier(),
                    steps=steps,
                    created=int(created.timestamp()) if created else 0,
                    updated=int(updated.timestamp()) if updated else 0,
                ),
                running=antigravity_is_running(),
            )
        except AntigravityIndexLocked as exc:
            # The conversation is on disk and complete. Saying "imported" and
            # leaving it unlisted would be the exact failure this milestone was
            # built to prevent, so it is reported instead.
            yield ImportEvent(
                kind="warning",
                conversation_id=identifier,
                message=f"written, but not added to Antigravity's list: {exc}",
            )

        # Set here rather than assumed to exist. A7b.8 was a whole phase spent
        # on a field that was populated onto an object and then discarded, and
        # ledger #220 was the same lesson again: setting the field and
        # recording it are two halves, and a document promising provenance is
        # untrue without both.
        conversation.provenance = Provenance(
            original_tool=conversation.source_tool,
            imported_into=TOOL,
            imported_at=datetime.now(UTC),
            ferry_version=__version__,
            lossy=True,
            conversion_notes=[*assess(conversation, TOOL).notes, *BUILD_NOTES],
        )
        provenance_store.record(
            TOOL,
            conversation.id,
            conversation.provenance,
            written=provenance_store.fingerprint(destination),
            title=conversation.title,
        )

        yield ImportEvent(
            kind="progress",
            conversation_id=identifier,
            message=f"built {destination.name} from {conversation.source_tool}, {steps} steps",
        )

    def _write(
        self,
        bundle: Bundle,
        conversation_id: UUID,
        original: Path,
        destination: Path,
        options: ImportOptions,
        remapper: PathRemapper,
    ) -> str:
        """Put one conversation in place, and report what was done.

        The database is remapped in a temporary file and moved into place only
        once it is complete, so an interrupted import cannot leave Antigravity
        holding a half-written conversation.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)

        if options.backup and destination.exists():
            # Into ~/.ferry/backups, not beside the original. A `.bak` file left
            # in `conversations/` is a file Antigravity may try to open, and it
            # is invisible to anyone looking for their backups where Ferry says
            # backups are.
            back_up(destination, TOOL)

        staged = destination.with_name(destination.name + ".ferry-tmp")
        shutil.copyfile(original, staged)
        report = remap_database(staged, remapper)
        staged.replace(destination)

        # Sidecars SQLite left beside a database this import replaced would
        # describe the conversation that used to be there.
        for suffix in ("-wal", "-shm"):
            stale = destination.with_name(destination.name + suffix)
            if stale.exists():
                stale.unlink()

        self._restore_project(bundle, conversation_id, remapper)
        self._restore_attachments(bundle, conversation_id)

        detail = f"wrote {destination.name}"
        if report.blobs_changed:
            detail += f", {report.blobs_changed} blobs path-remapped"
        return detail

    def _restore_project(
        self, bundle: Bundle, conversation_id: UUID, remapper: PathRemapper
    ) -> None:
        sidecar = bundle.source_raw_sidecar_dir(conversation_id) / PROJECT_SIDECAR
        if not sidecar.is_file():
            return
        try:
            document = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(document, dict):
            write_project(document, remapper, self._env)

    def _restore_attachments(self, bundle: Bundle, conversation_id: UUID) -> None:
        """Put uploaded images back where Antigravity looks for them.

        Under the *conversation's* brain directory, which is named by the
        conversation id, so a restored conversation finds its own uploads.
        """
        try:
            conversation = bundle.load_conversation(conversation_id)
        except Exception:  # noqa: BLE001 - an unreadable record is not worth failing the import
            return
        if not conversation.attachments:
            return

        directory = ag_paths.brain_dir(str(conversation_id), self._env) / ".user_uploaded"
        directory.mkdir(parents=True, exist_ok=True)
        for attachment in conversation.attachments:
            source = bundle.root / attachment.bundle_path
            if not source.is_file():
                continue
            target = directory / attachment.filename
            if not target.exists():
                shutil.copy2(source, target)
