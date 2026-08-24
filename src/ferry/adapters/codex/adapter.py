"""The Codex adapter: detect, export, import.

Reads ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<stamp>-<uuid>.jsonl`` and
writes it back. The same safety rules as every adapter apply — export never
touches the source, import never guesses a working directory — with one
constraint the others do not have:

**Nothing here may read a whole file.** A single rollout was 53 MB on the probe
machine and six conversations totalled 121 MB; public reports describe files an
order of magnitude larger again. Reading streams a line at a time, and the CLI
is warned before it exports something enormous.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID, uuid5

from ferry import __version__
from ferry.adapters.base import (
    Adapter,
    DetectResult,
    ExportEvent,
    ImportEvent,
    ImportOptions,
)
from ferry.adapters.census import census, jsonl_holds
from ferry.adapters.claude_code.writer import remap_prefix
from ferry.adapters.codex import paths as cx_paths
from ferry.adapters.codex.reader import SessionRead, read_rollout
from ferry.adapters.codex.writer import REBUILD_NOTES, Rebuild, rollout_lines, rollout_stamp
from ferry.adapters.dedup import compare_duplicate
from ferry.adapters.formatcheck import FormatCheck
from ferry.core import Bundle, Manifest, SourceMachine
from ferry.core.manifest import OSName
from ferry.ucs import Attachment, Conversation, Provenance, ToolName

__all__ = ["LARGE_SESSION_BYTES", "TOOL", "CodexAdapter"]

TOOL: Final[ToolName] = "codex"

_PASTED_NAMESPACE = UUID("6ba7b814-9dad-11d1-80b4-00c04fd430c8")

LARGE_SESSION_BYTES: Final = 100 * 1024 * 1024
"""Warn above this. PLAN.md §5 M4 asks for a size guard; this is its threshold."""

_OS_NAMES: dict[str, OSName] = {"Windows": "win32", "Darwin": "darwin", "Linux": "linux"}


_MESSAGE_ROLES = frozenset({"user", "assistant"})


def _is_message(record: dict[str, object]) -> bool:
    """Whether a rollout record is a turn somebody would see.

    A rollout always opens with ``session_meta`` and ``turn_context``, which
    say nothing about whether anyone spoke. The turns themselves are
    ``response_item`` records whose payload carries a role.
    """
    payload = record.get("payload")
    return isinstance(payload, dict) and payload.get("role") in _MESSAGE_ROLES


SAMPLE_RECORDS: Final = 200
"""How many rollout records the format check reads before deciding."""


def check_format(path: Path) -> FormatCheck:
    """Whether Codex still writes rollouts the way this adapter reads them.

    A rollout's turns are ``payload`` objects carrying a role and content. If
    either moved, every conversation would export as a series of empty turns
    and look perfectly successful doing it.
    """
    seen = intact = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if seen >= SAMPLE_RECORDS:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict) or payload.get("role") not in _MESSAGE_ROLES:
                    continue
                seen += 1
                if payload.get("content") is not None:
                    intact += 1
    except OSError:
        return FormatCheck(checked=False)

    if not seen:
        return FormatCheck(checked=False)
    if intact * 2 < seen:
        return FormatCheck(
            checked=True,
            findings=[
                f"{seen - intact} of {seen} sampled turns have no content where Ferry looks for it"
            ],
        )
    return FormatCheck(checked=True)


class CodexAdapter(Adapter):
    """Ferry's reader and writer for OpenAI Codex conversation history."""

    name = TOOL
    display_name = "OpenAI Codex"

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self._env = env

    # ---------- detect ----------

    def detect(self) -> DetectResult:
        notes: list[str] = []
        try:
            home = cx_paths.codex_home(self._env)
            sessions = cx_paths.sessions_dir(self._env)
        except (OSError, RuntimeError) as exc:
            return DetectResult(installed=False, notes=[f"could not resolve home directory: {exc}"])

        if not sessions.is_dir():
            notes.append(f"no session directory at {sessions}")
            if home.is_dir():
                notes.append(f"{home} exists but holds no sessions yet")
            return DetectResult(installed=False, notes=notes)

        try:
            rollouts = cx_paths.rollout_files(self._env)
        except OSError as exc:
            return DetectResult(installed=False, notes=[f"cannot read {sessions}: {exc}"])

        counted = census(
            [(path.stem, path) for path in rollouts],
            lambda _id, path: jsonl_holds(path, _is_message),
        )

        total = sum(path.stat().st_size for path in rollouts)
        notes.append(f"{total / 1024 / 1024:.0f} MB of transcripts at {sessions}")
        notes.extend(f"{line} (of {counted.files} rollouts)" for line in counted.notes()[:1])
        notes.extend(counted.notes()[1:])
        databases = cx_paths.state_databases(self._env)
        if databases:
            notes.append(f"state database: {databases[-1].name}")

        version = self._version_from(rollouts)
        checked = (
            check_format(max(rollouts, key=lambda path: path.stat().st_mtime))
            if rollouts
            else FormatCheck()
        )

        return DetectResult(
            installed=bool(rollouts),
            version=version,
            data_paths=[sessions],
            conversation_count_estimate=counted.conversations,
            notes=notes,
            caveats=checked.caveats(self.display_name, version) if rollouts else [],
        )

    @staticmethod
    def _version_from(rollouts: list[Path]) -> str | None:
        """The Codex version that wrote the most recent transcript.

        Reads only the first line, which is always ``session_meta`` and carries
        ``cli_version``. Never parses the rest — one of these files is 53 MB.
        """
        for path in sorted(rollouts, key=lambda p: (p.stat().st_mtime, p.name), reverse=True):
            try:
                with path.open(encoding="utf-8") as handle:
                    first = handle.readline()
            except OSError:
                continue
            import json

            try:
                record = json.loads(first)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload") if isinstance(record, dict) else None
            if isinstance(payload, dict):
                version = payload.get("cli_version")
                if isinstance(version, str) and version:
                    return version
        return None

    # ---------- export ----------

    def export(self, dest_bundle_dir: Path) -> Iterator[ExportEvent]:
        bundle = self._open_bundle(dest_bundle_dir)
        rollouts = cx_paths.rollout_files(self._env)
        if not rollouts:
            yield ExportEvent(
                kind="error",
                message=f"no rollouts under {cx_paths.sessions_dir(self._env)}",
            )
            return

        total = sum(path.stat().st_size for path in rollouts)
        yield ExportEvent(
            kind="started",
            message=f"{len(rollouts)} sessions, {total / 1024 / 1024:.0f} MB to read",
        )

        exported = 0
        for path in rollouts:
            for event in self._export_one(bundle, path):
                if event.kind == "progress":
                    exported += 1
                yield event
        yield ExportEvent(kind="done", message=f"{exported} of {len(rollouts)} sessions exported")

    def _export_one(self, bundle: Bundle, path: Path) -> Iterator[ExportEvent]:
        conversation_id = cx_paths.thread_id_of(path)
        if conversation_id is None:
            yield ExportEvent(kind="skipped", message=f"{path.name}: not a rollout file")
            return
        if bundle.has_conversation(conversation_id):
            # Re-reads the rollout, which can be 53 MB -- but only on the
            # duplicate path, which real data hits close to never. Skipping on
            # the id alone would keep whichever copy was read first. See
            # adapters/dedup.
            try:
                incoming = read_rollout(path, warn_over_bytes=LARGE_SESSION_BYTES).conversation
                existing = bundle.load_conversation(conversation_id)
            except (OSError, ValueError):
                incoming = existing = None
            verdict = compare_duplicate(conversation_id, incoming, existing, path.name)
            if verdict.warning:
                yield ExportEvent(
                    kind="warning",
                    conversation_id=str(conversation_id),
                    message=verdict.warning,
                )
            yield ExportEvent(
                kind="skipped", conversation_id=str(conversation_id), message=verdict.message
            )
            return

        size = path.stat().st_size
        if size > LARGE_SESSION_BYTES:
            yield ExportEvent(
                kind="warning",
                conversation_id=str(conversation_id),
                message=f"large session: {size / 1024 / 1024:.0f} MB",
            )

        try:
            found = read_rollout(path, warn_over_bytes=LARGE_SESSION_BYTES)
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

        for pending in found.attachments:
            bundle.add_attachment_bytes(conversation_id, pending.data, pending.record)

        pasted = self._pasted_files(bundle, conversation_id, found)
        bundle.add_conversation(found.conversation)

        detail = f"{len(found.conversation.messages)} messages"
        if found.attachments:
            detail += f", {len(found.attachments)} images"
        if pasted:
            detail += f", {pasted} pasted files"
        yield ExportEvent(kind="progress", conversation_id=str(conversation_id), message=detail)

    def _pasted_files(self, bundle: Bundle, conversation_id: UUID, found: SessionRead) -> int:
        """Carry the files Codex keeps outside the transcript.

        Text pasted into a conversation is written to
        ``attachments/<uuid>/pasted-text.txt`` and referenced only by a path
        inside the message prose. **Twelve of twenty-one such files on the probe
        machine had their contents nowhere in any transcript**, the largest
        3.4 MB, so leaving them behind loses real conversation material.

        The path inside the message is not rewritten -- Ferry does not edit what
        anyone said -- so on another machine that sentence still names the old
        location. The file itself travels and is restored, which is the part
        that would otherwise be gone for good.
        """
        files = cx_paths.attachment_files(found.mentioned_ids, self._env)
        if not files or found.conversation is None:
            return 0
        root = cx_paths.attachment_dir(self._env)
        for path in files:
            try:
                data = path.read_bytes()
            except OSError:
                continue
            relative = path.relative_to(root).as_posix()
            attachment = Attachment(
                id=uuid5(_PASTED_NAMESPACE, f"{conversation_id}:{relative}"),
                # The relative path, not the bare basename: every one of these
                # is called pasted-text.txt, and the directory is the only thing
                # that distinguishes them or says where to put them back.
                filename=relative,
                mime_type="text/plain",
                bundle_path=(
                    f"attachments/{conversation_id}/"
                    f"{uuid5(_PASTED_NAMESPACE, f'{conversation_id}:{relative}')}.txt"
                ),
                sha256=hashlib.sha256(data).hexdigest(),
            )
            bundle.add_attachment_bytes(conversation_id, data, attachment)
            found.conversation.attachments.append(attachment)
        return len(files)

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
                    os=_OS_NAMES.get(platform.system(), "linux"),
                    user_home=str(Path.home()),
                ),
            ),
            force=True,
        )

    # ---------- import ----------

    def import_(self, bundle_dir: Path, options: ImportOptions) -> Iterator[ImportEvent]:
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

        rebuild = Rebuild(
            cwd=target_cwd,
            thread_id=conversation_id,
            images=self._images(bundle, conversation),
        )
        payload = rollout_lines(conversation, rebuild)
        if payload is None:
            yield ImportEvent(
                kind="error",
                conversation_id=cid,
                message=(
                    "bundle carries no session_meta header; Codex rejects a rollout "
                    "without one, so nothing was written"
                ),
            )
            return

        created = conversation.created_at
        destination = (
            cx_paths.sessions_dir(self._env)
            / f"{created.year:04d}"
            / f"{created.month:02d}"
            / f"{created.day:02d}"
            / cx_paths.rollout_name(conversation_id, rollout_stamp(created))
        )

        if destination.exists() and options.on_conflict == "skip":
            yield ImportEvent(
                kind="skipped", conversation_id=cid, message=f"already at {destination.name}"
            )
            return

        for note in REBUILD_NOTES:
            yield ImportEvent(kind="warning", conversation_id=cid, message=note)

        if conversation.source_tool != TOOL:
            conversation.provenance = Provenance(
                original_tool=conversation.source_tool,
                imported_into=TOOL,
                imported_at=datetime.now(UTC),
                ferry_version=__version__,
                lossy=True,
                conversion_notes=list(REBUILD_NOTES),
            )

        if options.dry_run:
            yield ImportEvent(
                kind="progress",
                conversation_id=cid,
                message=f"would write {len(payload):,} bytes to {destination}",
            )
            return

        if options.backup and destination.exists():
            backup = self._back_up(destination)
            yield ImportEvent(
                kind="warning", conversation_id=cid, message=f"existing file backed up to {backup}"
            )

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            tmp = destination.with_name(destination.name + ".ferry-tmp")
            tmp.write_bytes(payload)
            tmp.replace(destination)
        except OSError as exc:
            yield ImportEvent(kind="error", conversation_id=cid, message=f"write failed: {exc}")
            return

        restored = self._restore_pasted(bundle, conversation)
        detail = f"{len(conversation.messages)} messages to {destination.name}"
        if restored:
            detail += f", {restored} pasted files restored"
        yield ImportEvent(kind="progress", conversation_id=cid, message=detail)

    def _restore_pasted(self, bundle: Bundle, conversation: Conversation) -> int:
        """Put pasted files back under this machine's attachments directory.

        Restored to the same ``<uuid>/<name>`` they came from, so a conversation
        that mentions one by path finds it in the same relative place. The path
        written in the message still names the *old* machine's home -- message
        text is never edited -- so the sentence stays stale even though the file
        is here. That is the honest trade: the content survives, the reference
        does not.
        """
        root = cx_paths.attachment_dir(self._env)
        restored = 0
        for attachment in conversation.attachments:
            if attachment.mime_type != "text/plain" or "/" not in attachment.filename:
                continue
            source = bundle.root / attachment.bundle_path
            if not source.is_file():
                continue
            destination = root / attachment.filename
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            except OSError:
                continue
            restored += 1
        return restored

    @staticmethod
    def _images(bundle: Bundle, conversation: Conversation) -> dict[UUID, tuple[Attachment, bytes]]:
        found: dict[UUID, tuple[Attachment, bytes]] = {}
        for attachment in conversation.attachments:
            try:
                found[attachment.id] = (
                    attachment,
                    (bundle.root / attachment.bundle_path).read_bytes(),
                )
            except OSError:
                continue
        return found

    @staticmethod
    def _back_up(destination: Path) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = Path.home() / ".ferry" / "backups" / stamp / destination.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(destination, backup)
        return backup
