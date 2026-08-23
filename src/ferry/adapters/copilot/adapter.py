"""The Copilot Chat adapter: detect and export.

Reads VS Code's chat transcripts from both stores -- the per-workspace one and
the empty-window one -- replays each file's deltas, and writes UCS.

``import_`` is not here yet. It needs the ``chat.ChatSessionStore.index`` in
the right ``state.vscdb``, and there are two of those: the global one lists
empty-window conversations and each workspace has its own. Writing a transcript
without its index entry produces a conversation VS Code will not show, which is
worse than not importing it, so that half waits until it can be done properly.
"""

from __future__ import annotations

import platform
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from ferry import __version__
from ferry.adapters.base import (
    Adapter,
    DetectResult,
    ExportEvent,
    ImportEvent,
    ImportOptions,
)
from ferry.adapters.copilot import paths as cp_paths
from ferry.adapters.copilot.reader import read_session
from ferry.core import Bundle, Manifest, SourceMachine
from ferry.core.manifest import OSName
from ferry.ucs import ToolName

__all__ = ["TOOL", "CopilotAdapter"]

TOOL: Final[ToolName] = "copilot"

_OS_NAMES: dict[str, OSName] = {"Windows": "win32", "Darwin": "darwin", "Linux": "linux"}


class CopilotAdapter(Adapter):
    """Ferry's reader for GitHub Copilot Chat history in VS Code."""

    name = TOOL
    display_name = "GitHub Copilot Chat"

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self._env = env

    # ---------- detect ----------

    def detect(self) -> DetectResult:
        notes: list[str] = []
        try:
            user = cp_paths.user_dir(self._env)
        except (OSError, RuntimeError) as exc:
            return DetectResult(installed=False, notes=[f"could not resolve VS Code data: {exc}"])

        if not user.is_dir():
            return DetectResult(installed=False, notes=[f"no VS Code user directory at {user}"])

        try:
            sessions = cp_paths.session_files(self._env)
        except OSError as exc:
            return DetectResult(installed=False, notes=[f"cannot read {user}: {exc}"])

        if not sessions:
            return DetectResult(
                installed=False,
                data_paths=[user],
                notes=[f"{user} exists but holds no chat sessions yet"],
            )

        workspaces = {key for key, _ in sessions if key}
        total = sum(path.stat().st_size for _, path in sessions)
        notes.append(f"{total / 1024:.0f} KB of transcripts at {user}")
        if workspaces:
            notes.append(f"{len(workspaces)} workspaces")
        empty = sum(1 for key, _ in sessions if not key)
        if empty:
            notes.append(f"{empty} started with no folder open")
        notes.append(
            "Copilot Chat storage is undocumented and may change with VS Code updates. "
            "Tested against VS Code 1.134.0."
        )

        return DetectResult(
            installed=True,
            data_paths=[user],
            conversation_count_estimate=len(sessions),
            notes=notes,
        )

    # ---------- export ----------

    def export(self, dest_bundle_dir: Path) -> Iterator[ExportEvent]:
        bundle = self._open_bundle(dest_bundle_dir)
        sessions = cp_paths.session_files(self._env)
        if not sessions:
            yield ExportEvent(
                kind="error",
                message=f"no chat sessions under {cp_paths.user_dir(self._env)}",
            )
            return

        yield ExportEvent(kind="started", message=f"{len(sessions)} chat sessions to read")

        exported = 0
        for key, path in sessions:
            for event in self._export_one(bundle, key, path):
                if event.kind == "progress":
                    exported += 1
                yield event
        yield ExportEvent(kind="done", message=f"{exported} of {len(sessions)} sessions exported")

    def _export_one(self, bundle: Bundle, key: str, path: Path) -> Iterator[ExportEvent]:
        conversation_id = cp_paths.session_id_of(path)
        if conversation_id is None:
            yield ExportEvent(kind="skipped", message=f"{path.name}: not a conversation file")
            return
        if bundle.has_conversation(conversation_id):
            yield ExportEvent(
                kind="skipped", conversation_id=str(conversation_id), message="already in bundle"
            )
            return

        found = read_session(path, key, self._env)
        for warning in found.warnings:
            yield ExportEvent(kind="warning", conversation_id=str(conversation_id), message=warning)

        if found.conversation is None:
            # An empty chat is the common case here, not a fault: VS Code
            # writes a session file the moment a chat panel opens, whether or
            # not anyone types into it.
            yield ExportEvent(
                kind="skipped",
                conversation_id=str(conversation_id),
                message=found.notes[0] if found.notes else "no messages",
            )
            return

        for image in found.images:
            bundle.add_attachment_bytes(conversation_id, image.data, image.record)
        bundle.add_conversation(found.conversation)

        detail = f"{len(found.conversation.messages)} messages"
        if found.images:
            detail += f", {len(found.images)} images"
        yield ExportEvent(kind="progress", conversation_id=str(conversation_id), message=detail)

        for note in found.notes:
            yield ExportEvent(kind="warning", conversation_id=str(conversation_id), message=note)

    # ---------- import ----------

    def import_(self, bundle_dir: Path, options: ImportOptions) -> Iterator[ImportEvent]:
        yield ImportEvent(
            kind="error",
            message=(
                "Importing into Copilot Chat is not built yet. A transcript written "
                "without its entry in chat.ChatSessionStore.index is a conversation "
                "VS Code will never show."
            ),
        )

    # ---------- shared ----------

    def _open_bundle(self, dest: Path) -> Bundle:
        """Open the destination bundle, creating it if this is a fresh export."""
        if (dest / "manifest.json").is_file():
            return Bundle.open(dest)
        return Bundle.create(dest, self._manifest())

    def _manifest(self) -> Manifest:
        return Manifest(
            created_at=datetime.now(UTC),
            created_by=f"ferry {__version__}",
            source_machine=SourceMachine(
                os=_OS_NAMES.get(platform.system(), "linux"),
                hostname=platform.node() or "unknown",
            ),
            tools_included=[TOOL],
        )
