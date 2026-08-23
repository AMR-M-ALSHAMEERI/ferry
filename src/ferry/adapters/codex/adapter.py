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

import platform
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID

from ferry import __version__
from ferry.adapters.base import (
    Adapter,
    DetectResult,
    ExportEvent,
    ImportEvent,
    ImportOptions,
)
from ferry.adapters.claude_code.writer import remap_prefix
from ferry.adapters.codex import paths as cx_paths
from ferry.adapters.codex.reader import read_rollout
from ferry.adapters.codex.writer import REBUILD_NOTES, Rebuild, rollout_lines, rollout_stamp
from ferry.core import Bundle, Manifest, SourceMachine
from ferry.core.manifest import OSName
from ferry.ucs import Attachment, Conversation, Provenance, ToolName

__all__ = ["LARGE_SESSION_BYTES", "TOOL", "CodexAdapter"]

TOOL: Final[ToolName] = "codex"

LARGE_SESSION_BYTES: Final = 100 * 1024 * 1024
"""Warn above this. PLAN.md §5 M4 asks for a size guard; this is its threshold."""

_OS_NAMES: dict[str, OSName] = {"Windows": "win32", "Darwin": "darwin", "Linux": "linux"}


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

        total = sum(path.stat().st_size for path in rollouts)
        notes.append(f"{total / 1024 / 1024:.0f} MB of transcripts at {sessions}")
        databases = cx_paths.state_databases(self._env)
        if databases:
            notes.append(f"state database: {databases[-1].name}")

        return DetectResult(
            installed=bool(rollouts),
            version=self._version_from(rollouts),
            data_paths=[sessions],
            conversation_count_estimate=len(rollouts),
            notes=notes,
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
            yield ExportEvent(
                kind="skipped", conversation_id=str(conversation_id), message="already in bundle"
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
        bundle.add_conversation(found.conversation)

        detail = f"{len(found.conversation.messages)} messages"
        if found.attachments:
            detail += f", {len(found.attachments)} images"
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

        yield ImportEvent(
            kind="progress",
            conversation_id=cid,
            message=f"{len(conversation.messages)} messages to {destination.name}",
        )

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
