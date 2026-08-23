"""The Claude Code adapter: detect, export, import.

Reads ``~/.claude/projects/<mangled-cwd>/<session-uuid>.jsonl`` and writes it
back. Everything tool-specific lives in the three modules beside this one --
:mod:`paths` knows where the data is, :mod:`reader` knows how to read a
transcript, :mod:`writer` knows how to write one. This file is the orchestration
and the safety rules.

Two of those rules are worth stating where they are enforced:

**Export never writes to the source.** Not a lock file, not a marker, nothing.
The self-check script checksums the whole source tree before and after and
asserts it did not move.

**Import never guesses a working directory.** The project directory name is
derived from the target machine's path, never decoded from the old name --
the mangling is lossy and cannot be reversed (see :mod:`paths`).
"""

from __future__ import annotations

import json
import platform
import re
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from ferry import __version__
from ferry.adapters.base import (
    Adapter,
    DetectResult,
    ExportEvent,
    ImportEvent,
    ImportOptions,
)
from ferry.adapters.claude_code import paths as cc_paths
from ferry.adapters.claude_code.reader import read_session
from ferry.adapters.claude_code.writer import (
    SYNTHESIS_NOTES,
    Remap,
    missing_images,
    remap_prefix,
    remap_record,
    session_lines,
    synthesize_records,
)
from ferry.core import Bundle, Manifest, SourceMachine
from ferry.core.manifest import OSName
from ferry.ucs import Conversation, Provenance, ToolName

__all__ = ["SIDECAR_SUBDIR", "TOOL", "ClaudeCodeAdapter"]

TOOL: Final[ToolName] = "claude-code"
"""This adapter's UCS tool name.

A constant rather than a bare string on the class, because ``Adapter.name``
is typed ``str`` for every tool and UCS wants the narrower literal. Reaching
for ``self.name`` where a ``ToolName`` is required only type-checks by
accident.
"""

SIDECAR_SUBDIR = "tool-results"
"""Directory name Claude Code spills oversized tool output into."""

_VERSION_FIELD = re.compile(r'"version"\s*:\s*"([^"]+)"')

_OS_NAMES: dict[str, OSName] = {"Windows": "win32", "Darwin": "darwin", "Linux": "linux"}


def _os_name() -> OSName:
    return _OS_NAMES.get(platform.system(), "linux")


class ClaudeCodeAdapter(Adapter):
    """Ferry's reader and writer for Claude Code conversation history."""

    name = TOOL
    display_name = "Claude Code"

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self._env = env

    # ---------- detect ----------

    def detect(self) -> DetectResult:
        """Look for Claude Code's data. Never raises -- see the base class."""
        notes: list[str] = []
        try:
            root = cc_paths.config_root(self._env)
            projects = cc_paths.projects_dir(self._env)
        except (OSError, RuntimeError) as exc:
            return DetectResult(installed=False, notes=[f"could not resolve home directory: {exc}"])

        if not projects.is_dir():
            notes.append(f"no conversation directory at {projects}")
            if root.is_dir():
                notes.append(f"{root} exists but holds no projects yet")
            return DetectResult(installed=False, notes=notes)

        try:
            project_dirs = sorted(p for p in projects.iterdir() if p.is_dir())
        except OSError as exc:
            return DetectResult(installed=False, notes=[f"cannot read {projects}: {exc}"])

        sessions: list[Path] = []
        for project in project_dirs:
            try:
                sessions.extend(cc_paths.session_files(project))
            except OSError as exc:
                notes.append(f"cannot read {project.name}: {exc}")

        notes.append(f"{len(project_dirs)} project directories at {projects}")
        if self._env is None and not (Path.home() / ".claude.json").is_file():
            notes.append("~/.claude.json not found; per-project settings will not be carried")

        return DetectResult(
            installed=bool(sessions),
            version=self._version_from(sessions),
            data_paths=[projects],
            conversation_count_estimate=len(sessions),
            notes=notes,
        )

    @staticmethod
    def _version_from(sessions: list[Path]) -> str | None:
        """The Claude Code version that last wrote a transcript.

        Read out of the data rather than off the filesystem: the app is
        installed in a different place on every platform, and a version that
        never wrote anything is not the version that produced this history.

        The *last* version in the *most recently touched* transcript: a session
        that spans an upgrade carries both, and the one that matters is the one
        writing now. Matched with a regex rather than parsed as JSON because
        ``detect()`` runs every time Ferry starts and a real transcript runs to
        several megabytes.
        """
        for session in sorted(sessions, key=lambda p: (p.stat().st_mtime, p.name), reverse=True):
            found: str | None = None
            try:
                with session.open(encoding="utf-8") as handle:
                    for line in handle:
                        match = _VERSION_FIELD.search(line)
                        if match:
                            found = match.group(1)
            except OSError:
                continue
            if found:
                return found
        return None

    # ---------- export ----------

    def export(self, dest_bundle_dir: Path) -> Iterator[ExportEvent]:
        """Read every session into ``dest_bundle_dir`` as a Ferry bundle.

        Resumable: a conversation whose UCS file is already present is skipped,
        so re-running after an interruption picks up where it stopped rather
        than rewriting what is already there.
        """
        bundle = self._open_bundle(dest_bundle_dir)
        projects = cc_paths.projects_dir(self._env)
        if not projects.is_dir():
            yield ExportEvent(kind="error", message=f"no conversation directory at {projects}")
            return

        sessions = [
            session
            for project in sorted(p for p in projects.iterdir() if p.is_dir())
            for session in cc_paths.session_files(project)
        ]
        yield ExportEvent(kind="started", message=f"{len(sessions)} sessions found")

        exported = 0
        for session in sessions:
            for event in self._export_one(bundle, session):
                if event.kind == "progress":
                    exported += 1
                yield event
        yield ExportEvent(kind="done", message=f"{exported} of {len(sessions)} sessions exported")

    def _export_one(self, bundle: Bundle, session: Path) -> Iterator[ExportEvent]:
        try:
            conversation_id = UUID(session.stem)
        except ValueError:
            yield ExportEvent(kind="skipped", message=f"{session.name}: not a session file")
            return
        if bundle.has_conversation(conversation_id):
            yield ExportEvent(
                kind="skipped",
                conversation_id=str(conversation_id),
                message="already in bundle",
            )
            return

        try:
            found = read_session(session)
        except OSError as exc:
            yield ExportEvent(
                kind="error", conversation_id=str(conversation_id), message=f"unreadable: {exc}"
            )
            return

        for warning in found.warnings:
            yield ExportEvent(kind="warning", conversation_id=str(conversation_id), message=warning)
        if found.conversation is None:
            yield ExportEvent(
                kind="skipped",
                conversation_id=str(conversation_id),
                message=found.notes[0] if found.notes else "no messages",
            )
            return

        # Attachments and the original bytes land before the UCS file does, so
        # a crash mid-export leaves a conversation missing rather than a
        # conversation present with its pieces absent. The UCS file is the
        # marker that says "this one is finished" -- has_conversation() above
        # is what makes the export resumable, and it must not lie.
        for pending in found.attachments:
            bundle.add_attachment_bytes(conversation_id, pending.data, pending.record)
        bundle.add_source_raw(conversation_id, session)
        for spilled in found.sidecars:
            bundle.add_source_raw_sidecar(
                conversation_id, spilled, f"{SIDECAR_SUBDIR}/{spilled.name}"
            )
        bundle.add_conversation(found.conversation)

        detail = f"{len(found.conversation.messages)} messages"
        if found.attachments:
            detail += f", {len(found.attachments)} attachments"
        if found.sidecars:
            detail += f", {len(found.sidecars)} spilled tool outputs"
        yield ExportEvent(kind="progress", conversation_id=str(conversation_id), message=detail)

    def _open_bundle(self, dest: Path) -> Bundle:
        if (dest / "manifest.json").is_file():
            return Bundle.open(dest)
        return Bundle.create(
            dest,
            Manifest(
                created_at=datetime.now(UTC),
                created_by=f"ferry v{__version__}",
                source_machine=SourceMachine(
                    hostname=platform.node() or None,
                    os=_os_name(),
                    user_home=str(Path.home()),
                ),
            ),
            force=True,
        )

    # ---------- import ----------

    def import_(self, bundle_dir: Path, options: ImportOptions) -> Iterator[ImportEvent]:
        """Write conversations from a bundle into this machine's Claude Code."""
        try:
            bundle = Bundle.open(bundle_dir)
        except Exception as exc:  # noqa: BLE001 - surfaced as an event, never a crash
            yield ImportEvent(kind="error", message=str(exc))
            return

        ids = bundle.list_conversations()
        yield ImportEvent(kind="started", message=f"{len(ids)} conversations in bundle")

        written = 0
        for conversation_id in ids:
            for event in self._import_one(bundle, conversation_id, options):
                if event.kind == "progress":
                    written += 1
                yield event
        verb = "would be written" if options.dry_run else "written"
        yield ImportEvent(kind="done", message=f"{written} of {len(ids)} {verb}")

    def _import_one(
        self, bundle: Bundle, conversation_id: UUID, options: ImportOptions
    ) -> Iterator[ImportEvent]:
        cid = str(conversation_id)
        try:
            conversation = bundle.load_conversation(conversation_id)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest
            yield ImportEvent(kind="error", conversation_id=cid, message=str(exc))
            return

        if conversation.source_tool != TOOL and not options.allow_cross_tool:
            yield ImportEvent(
                kind="skipped",
                conversation_id=cid,
                message=(
                    f"came from {conversation.source_tool}; "
                    "cross-tool import must be asked for explicitly"
                ),
            )
            return

        original = conversation.workspace.original_path
        target_cwd = remap_prefix(original, options.path_remap) if original else str(Path.cwd())
        if not original:
            yield ImportEvent(
                kind="warning",
                conversation_id=cid,
                message=f"bundle records no working directory; filing under {target_cwd}",
            )

        project = cc_paths.project_dir(target_cwd, self._env)
        destination = project / f"{conversation_id}.jsonl"
        sidecar_root = project / str(conversation_id) / SIDECAR_SUBDIR

        if destination.exists():
            if options.on_conflict == "skip":
                yield ImportEvent(
                    kind="skipped", conversation_id=cid, message=f"already at {destination.name}"
                )
                return
            if options.on_conflict == "rename":
                destination = self._free_name(destination)

        payload, notes = self._payload_for(bundle, conversation, target_cwd, sidecar_root)
        for note in notes:
            yield ImportEvent(kind="warning", conversation_id=cid, message=note)

        if conversation.source_tool != TOOL:
            conversation.provenance = Provenance(
                original_tool=conversation.source_tool,
                imported_into=TOOL,
                imported_at=datetime.now(UTC),
                ferry_version=__version__,
                lossy=True,
                conversion_notes=list(notes),
            )

        if options.dry_run:
            yield ImportEvent(
                kind="progress",
                conversation_id=cid,
                message=f"would write {len(payload)} bytes to {destination}",
            )
            return

        if options.backup and destination.exists():
            backup = self._back_up(destination)
            yield ImportEvent(
                kind="warning", conversation_id=cid, message=f"existing file backed up to {backup}"
            )

        try:
            self._write(destination, payload)
            restored = self._restore_sidecars(bundle, conversation_id, sidecar_root)
        except OSError as exc:
            yield ImportEvent(kind="error", conversation_id=cid, message=f"write failed: {exc}")
            return

        detail = f"{len(conversation.messages)} messages to {project.name}"
        if restored:
            detail += f", {restored} spilled tool outputs restored"
        yield ImportEvent(kind="progress", conversation_id=cid, message=detail)

    def _payload_for(
        self,
        bundle: Bundle,
        conversation: Conversation,
        target_cwd: str,
        sidecar_root: Path,
    ) -> tuple[bytes, list[str]]:
        """The JSONL to write, and everything that got lost producing it."""
        raw = bundle.source_raw_path(conversation.id)
        remap = Remap(
            old_cwd=conversation.workspace.original_path,
            new_cwd=target_cwd,
            new_sidecar_root=str(sidecar_root),
        )
        if raw.is_file() and conversation.source_tool == TOOL:
            records: list[dict[str, Any]] = []
            broken = 0
            with raw.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        broken += 1
                        continue
                    if isinstance(record, dict):
                        records.append(remap_record(record, remap))
                    else:
                        broken += 1
            notes = [f"{broken} unreadable lines dropped from the original"] if broken else []
            return session_lines(records), notes

        version = conversation.source_tool_version or f"ferry-{__version__}"
        image_bytes = self._attachment_bytes(bundle, conversation)
        notes = list(SYNTHESIS_NOTES)
        absent = missing_images(conversation, image_bytes)
        if absent:
            notes.append(f"{len(absent)} image blocks dropped: their bytes are not in the bundle")
        records = synthesize_records(
            conversation, cwd=target_cwd, version=version, image_bytes=image_bytes
        )
        return session_lines(records), notes

    @staticmethod
    def _attachment_bytes(bundle: Bundle, conversation: Conversation) -> dict[UUID, bytes]:
        """Read the conversation's attachments back out of the bundle.

        Claude Code stores images inline, so rebuilding a record means putting
        the bytes back into it. An attachment that has gone missing is skipped
        here and reported by the caller, rather than crashing an import over one
        absent picture.
        """
        found: dict[UUID, bytes] = {}
        for attachment in conversation.attachments:
            path = bundle.root / attachment.bundle_path
            try:
                found[attachment.id] = path.read_bytes()
            except OSError:
                continue
        return found

    @staticmethod
    def _free_name(destination: Path) -> Path:
        for suffix in range(1, 1000):
            candidate = destination.with_name(f"{destination.stem}-{suffix}{destination.suffix}")
            if not candidate.exists():
                return candidate
        raise OSError(f"no free filename beside {destination}")

    @staticmethod
    def _back_up(destination: Path) -> Path:
        """Copy the file about to be overwritten into ``~/.ferry/backups``."""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = Path.home() / ".ferry" / "backups" / stamp / destination.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(destination, backup)
        return backup

    @staticmethod
    def _write(destination: Path, payload: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(destination.name + ".ferry-tmp")
        tmp.write_bytes(payload)
        tmp.replace(destination)

    @staticmethod
    def _restore_sidecars(bundle: Bundle, conversation_id: UUID, sidecar_root: Path) -> int:
        spilled = bundle.list_source_raw_sidecars(conversation_id)
        if not spilled:
            return 0
        sidecar_root.mkdir(parents=True, exist_ok=True)
        for source in spilled:
            shutil.copyfile(source, sidecar_root / source.name)
        return len(spilled)
