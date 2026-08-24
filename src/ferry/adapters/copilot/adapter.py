"""The Copilot Chat adapter: detect and export.

Reads VS Code's chat transcripts from both stores -- the per-workspace one and
the empty-window one -- replays each file's deltas, and writes UCS.

Import writes the transcript **and** its entry in the matching
``chat.ChatSessionStore.index``, because a transcript without one is a
conversation VS Code will never display.

Where a conversation lands is decided rather than assumed. A bundle records the
workspace key it came from, and that key is a digest of a folder path and that
folder's creation time **on the machine it came from**. On any other machine it
names nothing. So: if the recorded workspace exists here, the conversation goes
back to it; otherwise it goes to the empty-window store, where VS Code shows it
without a folder. Ferry never creates a workspace directory to make a key fit.
"""

from __future__ import annotations

import platform
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
from ferry.adapters.census import Census, census
from ferry.adapters.copilot import paths as cp_paths
from ferry.adapters.copilot.deltas import replay_lines
from ferry.adapters.copilot.reader import read_session
from ferry.adapters.copilot.writer import (
    SessionStoreLocked,
    index_entry,
    index_lists,
    recorded_workspace_key,
    snapshot_line,
    upsert_index_entry,
    vs_code_is_running,
)
from ferry.adapters.dedup import compare_duplicate
from ferry.adapters.formatcheck import FormatCheck
from ferry.core import Bundle, Manifest, SourceMachine
from ferry.core.manifest import OSName
from ferry.ucs import Conversation, ToolName

__all__ = ["TESTED_VERSION", "TOOL", "CopilotAdapter"]

TOOL: Final[ToolName] = "copilot"

TESTED_VERSION: Final = "1.134.0"
"""The VS Code release this adapter was built and verified against."""


_OS_NAMES: dict[str, OSName] = {"Windows": "win32", "Darwin": "darwin", "Linux": "linux"}


def check_format(path: Path) -> FormatCheck:
    """Whether VS Code still writes chat transcripts the way this adapter reads them.

    A Copilot transcript is a **log of edits**, so the check has to replay it
    rather than look at it, then confirm the replay still produces a list of
    turns carrying the fields the reader looks for. If VS Code renamed
    ``message`` or ``response``, every conversation would export with no text
    and nothing anywhere would report an error.

    **An unrecognised record type is deliberately not a finding here.** Ferry
    already reports those during an export, naming the file and how many were
    skipped, which is more use than a line on the scan screen. What belongs
    here is only what would otherwise pass in silence.
    """
    findings: list[str] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            replayed = replay_lines(handle)
    except OSError:
        return FormatCheck(checked=False)

    turns = replayed.document.get("requests")
    if not isinstance(turns, list):
        return FormatCheck(
            checked=True,
            findings=[*findings, "the replayed transcript has no list of turns in it"],
        )
    if not turns:
        return FormatCheck(checked=False)

    recognisable = sum(
        1 for turn in turns if isinstance(turn, dict) and ("message" in turn or "response" in turn)
    )
    if recognisable * 2 < len(turns):
        findings.append(
            f"{len(turns) - recognisable} of {len(turns)} turns carry neither a question "
            "nor an answer where Ferry looks for them"
        )

    return FormatCheck(checked=True, findings=findings)


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

        counted = self._census(sessions)
        workspaces = {key for key, _ in sessions if key}
        total = sum(path.stat().st_size for _, path in sessions)
        notes.append(f"{total / 1024:.0f} KB of transcripts at {user}")
        if workspaces:
            notes.append(f"{len(workspaces)} workspaces")
        no_folder = sum(1 for key, _ in sessions if not key)
        if no_folder:
            notes.append(f"{no_folder} started with no folder open")
        notes.extend(f"{line} (of {counted.files} session files)" for line in counted.notes()[:1])
        notes.extend(counted.notes()[1:])
        version = cp_paths.vscode_version(self._env)

        # The newest transcript **that holds a conversation**. Most session
        # files here are empty -- VS Code writes one whenever a chat panel
        # opens -- and checking one of those reports "nothing to read" on a
        # machine with plenty to read.
        checked = FormatCheck()
        for _key, path in sorted(sessions, key=lambda item: item[1].stat().st_mtime, reverse=True):
            checked = check_format(path)
            if checked.checked:
                break

        return DetectResult(
            installed=True,
            version=version,
            data_paths=[user],
            conversation_count_estimate=counted.conversations,
            notes=notes,
            caveats=checked.caveats(self.display_name, version),
        )

    def _census(self, sessions: list[tuple[str, Path]]) -> Census:
        """How many of these files VS Code would actually list.

        Two things make that fewer than the number of files, and both grow
        with use: **VS Code writes a session file whenever a chat panel
        opens**, typed into or not, and a conversation open in two workspaces
        is stored twice under one id. On the machine this was written on, 18
        files are 5 conversations.

        Replaying to find out is affordable -- 18 files in 60 ms -- because
        these are small and a chat with messages is recognised in its first
        record.
        """
        return census(
            [(path.stem, path) for _key, path in sessions],
            lambda _id, path: self._has_messages(path),
        )

    @staticmethod
    def _has_messages(path: Path) -> bool:
        """Whether a transcript replays to a conversation with turns in it.

        It has to be replayed. A Copilot transcript is a log of edits, and
        every real one on this machine has an **empty** message list in its
        first record -- so the file's own opening says nothing about whether it
        holds a conversation.
        """
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                result = replay_lines(handle)
        except OSError:
            return True
        turns = result.document.get("requests")
        return isinstance(turns, list) and bool(turns)

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

    def _skip_duplicate(
        self, bundle: Bundle, key: str, path: Path, conversation_id: UUID
    ) -> Iterator[ExportEvent]:
        """Skip a conversation already in the bundle -- after checking it.

        The same session id really does appear under more than one workspace:
        one conversation here exists in two byte-different files holding
        identical content. Skipping the second is right, and stays right only
        while they agree -- see :mod:`ferry.adapters.dedup`.
        """
        try:
            incoming = read_session(path, key, self._env).conversation
            existing = bundle.load_conversation(conversation_id)
        except (OSError, ValueError):
            incoming = existing = None

        verdict = compare_duplicate(conversation_id, incoming, existing, path.name)
        if verdict.warning:
            yield ExportEvent(
                kind="warning", conversation_id=str(conversation_id), message=verdict.warning
            )
        yield ExportEvent(
            kind="skipped", conversation_id=str(conversation_id), message=verdict.message
        )

    def _export_one(self, bundle: Bundle, key: str, path: Path) -> Iterator[ExportEvent]:
        conversation_id = cp_paths.session_id_of(path)
        if conversation_id is None:
            yield ExportEvent(kind="skipped", message=f"{path.name}: not a conversation file")
            return
        if bundle.has_conversation(conversation_id):
            yield from self._skip_duplicate(bundle, key, path, conversation_id)
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
            yield ExportEvent(kind="note", conversation_id=str(conversation_id), message=note)

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

        if vs_code_is_running(self._env):
            # Warned rather than refused: the crude process check can be wrong,
            # and the real defence is that the index write fails on a locked
            # database instead of reporting a success VS Code would discard.
            yield ImportEvent(
                kind="warning",
                message=(
                    "VS Code appears to be running. It holds the chat list in memory "
                    "and rewrites it on exit, so close it before importing."
                ),
            )

        yield ImportEvent(kind="started", message=f"{len(conversations)} conversations to write")

        written = 0
        for conversation_id in conversations:
            for event in self._import_one(bundle, conversation_id, options):
                if event.kind == "progress":
                    written += 1
                yield event
        yield ImportEvent(kind="done", message=f"{written} of {len(conversations)} imported")

    def _import_one(
        self, bundle: Bundle, conversation_id: UUID, options: ImportOptions
    ) -> Iterator[ImportEvent]:
        conversation = bundle.load_conversation(conversation_id)
        if conversation.source_tool != TOOL:
            yield ImportEvent(
                kind="skipped",
                conversation_id=str(conversation_id),
                message=(
                    f"came from {conversation.source_tool}; "
                    "cross-tool import into Copilot Chat is M7b"
                ),
            )
            return

        destination, database, where = self._destination(conversation)
        transcript = destination / f"{conversation_id}.jsonl"

        if transcript.exists() or str(conversation_id) in index_lists(database):
            yield ImportEvent(
                kind="skipped",
                conversation_id=str(conversation_id),
                message="already in Copilot Chat",
            )
            return

        try:
            line = snapshot_line(conversation)
            entry = index_entry(conversation)
        except ValueError as exc:
            yield ImportEvent(
                kind="skipped", conversation_id=str(conversation_id), message=str(exc)
            )
            return

        # The index goes first. A transcript listed but missing is an empty
        # chat; a transcript present but unlisted is invisible, and the user
        # has no way to discover it exists.
        try:
            upsert_index_entry(database, entry)
        except SessionStoreLocked as exc:
            yield ImportEvent(kind="error", conversation_id=str(conversation_id), message=str(exc))
            return

        destination.mkdir(parents=True, exist_ok=True)
        temporary = transcript.with_suffix(".jsonl.ferry-tmp")
        temporary.write_text(line + "\n", encoding="utf-8", newline="\n")
        temporary.replace(transcript)

        yield ImportEvent(
            kind="progress",
            conversation_id=str(conversation_id),
            message=f"{len(conversation.messages)} messages into {where}",
        )

    def _destination(self, conversation: Conversation) -> tuple[Path, Path, str]:
        """Where this conversation can honestly be put, and which index lists it.

        The recorded workspace key only means something on the machine that
        produced it -- it is a digest of a folder path *and that folder's
        creation time*. Rather than create a directory to make the key fit,
        which would produce a workspace VS Code has never heard of, a
        conversation whose workspace is not here goes to the empty-window
        store and shows up without a folder.
        """
        key = recorded_workspace_key(conversation)
        if key:
            workspace = cp_paths.workspace_storage(self._env) / key
            if workspace.is_dir():
                return workspace / "chatSessions", workspace / "state.vscdb", f"workspace {key[:8]}"
        return (
            cp_paths.empty_window_dir(self._env),
            cp_paths.global_storage(self._env) / "state.vscdb",
            "the no-folder chat list",
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
