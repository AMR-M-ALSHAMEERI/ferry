"""Writing a conversation back into VS Code's chat storage.

Two things must happen together, and doing only the first is the failure this
module is shaped around:

1. The transcript goes to ``<store>/<uuid>.jsonl``.
2. An entry goes into ``chat.ChatSessionStore.index`` in the matching
   ``state.vscdb``.

**A transcript without its index entry is a conversation VS Code will never
show.** The file is there, the data is intact, and the chat list is empty --
which looks exactly like the import silently failing. So the index write is not
a finishing touch, it is half the operation.

There are **two** indexes. ``globalStorage/state.vscdb`` lists conversations
started with no folder open; each workspace has its own listing that
workspace's. Writing to the wrong one produces the same invisible result.

The transcript itself is written as a single ``kind: 0`` snapshot. Copilot
writes a snapshot followed by deltas because it is recording edits as they
happen; a conversation that is already complete has nothing to replay, and one
snapshot is what the replayer would arrive at anyway.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid5

from ferry.compact.catalogue import describe
from ferry.ucs import Conversation

__all__ = [
    "INDEX_KEY",
    "SessionStoreLocked",
    "index_entry",
    "snapshot_line",
    "upsert_index_entry",
]

INDEX_KEY = "chat.ChatSessionStore.index"
"""The ``ItemTable`` key holding the conversation list VS Code renders."""

_BUSY_TIMEOUT_SECONDS = 5.0


class SessionStoreLocked(RuntimeError):
    """The state database could not be written.

    Almost always means VS Code is running. Raised rather than retried, because
    a running VS Code holds this state in memory and rewrites it on exit -- so
    even a write that succeeded would be discarded, and reporting success would
    be a lie the user only discovers later.
    """


def _epoch_ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


_REQUEST_NAMESPACE: Final = UUID("f0d6c8b2-3f4a-5e6d-8c9b-0a1b2c3d4e5f")
"""Namespace for request and response ids, so re-importing the same
conversation produces the same ids rather than a fresh set each time."""


def snapshot_line(conversation: Conversation) -> str:
    """The whole conversation as one ``kind: 0`` record.

    Uses the document carried in ``source_raw``, which for this adapter is the
    complete replayed transcript -- so what is written back is what was read,
    not a reconstruction of it.

    ``sessionId`` is forced to match the filename. They agreed on every real
    file, but a transcript whose internal id disagrees with its name is the
    kind of thing that works until it suddenly does not.
    """
    document = dict(_document_of(conversation)) if _has_document(conversation) else None
    if document is None:
        # No original to replay, so one is built. See `synthesize_document`.
        document = synthesize_document(conversation)
    document["sessionId"] = str(conversation.id)
    return json.dumps({"kind": 0, "v": document}, ensure_ascii=False)


def _has_document(conversation: Conversation) -> bool:
    """Whether this conversation carries the document it came from."""
    raw = conversation.source_raw or {}
    return isinstance(raw.get("document"), dict)


def _document_of(conversation: Conversation) -> dict[str, Any]:
    raw = conversation.source_raw or {}
    document = raw.get("document")
    if not isinstance(document, dict):
        raise ValueError(
            "conversation carries no original Copilot document; "
            "cross-tool import into Copilot Chat is M7b"
        )
    return document


def recorded_workspace_key(conversation: Conversation) -> str:
    """The workspace this conversation came from, if it named one."""
    raw = conversation.source_raw or {}
    key = raw.get("workspace_key")
    return key if isinstance(key, str) else ""


def index_entry(conversation: Conversation) -> dict[str, Any]:
    """One row of the chat list, shaped as VS Code writes it.

    Field set taken from real entries in both indexes; see ``PROGRESS.md``
    §4.2. ``timing.created`` is required -- VS Code sorts the list by it, and
    an entry without one sorts unpredictably rather than failing visibly.
    """
    document = _document_of(conversation) if _has_document(conversation) else {}
    created = document.get("creationDate")
    created_ms = created if isinstance(created, int) else _epoch_ms(conversation.created_at)
    last_ms = _epoch_ms(conversation.updated_at)

    return {
        "sessionId": str(conversation.id),
        "title": conversation.title or "New Chat",
        "lastMessageDate": last_ms,
        "timing": {"created": created_ms},
        "initialLocation": document.get("initialLocation") or "panel",
        "hasPendingEdits": False,
        "isEmpty": not conversation.messages,
        "isExternal": False,
        "lastResponseState": 1,
        "permissionLevel": "default",
    }


def upsert_index_entry(database: Path, entry: dict[str, Any]) -> None:
    """Add or replace one conversation in a ``state.vscdb`` chat list.

    Read-modify-write inside a transaction. The value is a JSON blob holding
    every conversation, so a careless write drops the ones already there --
    which would delete a person's chat list while appearing to add to it.

    Creates the database and table if absent: a workspace that has never had a
    chat has no ``state.vscdb``, and that is a normal destination.
    """
    database.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(database, timeout=_BUSY_TIMEOUT_SECONDS)
    except sqlite3.Error as exc:  # pragma: no cover - depends on the filesystem
        raise SessionStoreLocked(f"cannot open {database.name}: {exc}") from exc

    try:
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS ItemTable "
                "(key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)"
            )
            row = connection.execute(
                "SELECT value FROM ItemTable WHERE key = ?", (INDEX_KEY,)
            ).fetchone()

            index: dict[str, Any] = {"version": 1, "entries": {}}
            if row is not None:
                try:
                    existing = json.loads(row[0])
                except (TypeError, ValueError):
                    existing = None
                if isinstance(existing, dict) and isinstance(existing.get("entries"), dict):
                    index = existing

            index["entries"][entry["sessionId"]] = entry
            # `OR REPLACE` explicitly, rather than relying on the table's own
            # conflict clause. VS Code declares `UNIQUE ON CONFLICT REPLACE`,
            # but depending on a detail of someone else's schema means a plain
            # `UNIQUE` in some future build fails the write instead.
            connection.execute(
                "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                (INDEX_KEY, json.dumps(index, ensure_ascii=False)),
            )
    except sqlite3.OperationalError as exc:
        raise SessionStoreLocked(
            f"{database.name} is locked - close VS Code and try again ({exc})"
        ) from exc
    finally:
        connection.close()


def index_lists(database: Path) -> set[str]:
    """The conversation ids a chat list already holds."""
    if not database.is_file():
        return set()
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    except sqlite3.Error:  # pragma: no cover - unreadable database
        return set()
    try:
        row = connection.execute(
            "SELECT value FROM ItemTable WHERE key = ?", (INDEX_KEY,)
        ).fetchone()
    except sqlite3.Error:
        return set()
    finally:
        connection.close()
    if row is None:
        return set()
    try:
        index = json.loads(row[0])
    except (TypeError, ValueError):
        return set()
    entries = index.get("entries") if isinstance(index, dict) else None
    return set(entries) if isinstance(entries, dict) else set()


def vs_code_is_running(env: os._Environ[str] | dict[str, str] | None = None) -> bool:
    """Whether a VS Code process appears to be holding the stores open.

    Deliberately crude and deliberately not authoritative: the real defence is
    that :func:`upsert_index_entry` raises when the database is locked. This
    exists so the user is warned *before* a long import rather than after it.
    """
    import shutil
    import subprocess  # noqa: S404 - reading a process list, no shell

    if os.name == "nt":
        executable = shutil.which("tasklist")
        if executable is None:  # pragma: no cover - present on every Windows
            return False
        command = [executable, "/FI", "IMAGENAME eq Code.exe", "/NH"]
    else:
        executable = shutil.which("pgrep")
        if executable is None:
            return False
        command = [executable, "-x", "code"]

    try:
        finished = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - platform dependent
        return False
    return "Code.exe" in finished.stdout if os.name == "nt" else finished.returncode == 0


def session_id_strings(ids: list[UUID]) -> set[str]:
    """Ids as the strings the index keys on."""
    return {str(value) for value in ids}


# --------------------------------------------------------------------------
# building a document for a conversation that never had one
# --------------------------------------------------------------------------

SYNTHESIS_NOTES: Final = (
    "rebuilt as a VS Code chat document; tool calls and their results are kept "
    "as readable text, not as calls Copilot can re-run",
)
"""What is unavoidably lost when a document is built rather than replayed."""

_RESULT_LIMIT: Final = 2_000
"""Characters of one tool result kept. A result is the largest thing in a
conversation by far and the least useful to read in full; the whole of it is
still in the bundle."""


def _rendered(block: Any, source_tool: str) -> str:
    """One UCS block as the markdown a person will read.

    **Nothing is fabricated as a Copilot construct.** VS Code has typed parts
    for tool invocations and for thinking, and Ferry does not emit them: they
    carry ids, timings and invocation state for calls that happened in another
    tool, and a call presented as one Copilot could re-run would be a lie the
    interface makes on Ferry's behalf. They are written as text, and marked.
    """
    kind = getattr(block, "type", "")
    if kind == "text":
        return str(block.text)
    if kind == "thinking":
        return f"> **[{source_tool}] thinking**\n>\n> " + str(block.text).replace("\n", "\n> ")
    if kind == "tool_use":
        call = describe(source_tool, block.name, block.input)
        detail = call.command or ", ".join(call.paths)
        return f"`[{block.name}]`" + (f" {detail}" if detail else "")
    if kind == "tool_result":
        text = block.output if isinstance(block.output, str) else json.dumps(block.output)
        clipped = text[:_RESULT_LIMIT]
        tail = (
            "\n(truncated; the whole result is in the bundle)" if len(text) > _RESULT_LIMIT else ""
        )
        return f"```\n{clipped}{tail}\n```"
    if kind == "image":
        return "`[image]`"
    return ""


def _message(text: str) -> dict[str, Any]:
    """One question, in the shape VS Code draws a question bubble from.

    **`text` alone is not enough, and it fails silently in the worst way.**
    Ferry first wrote text with an empty ``parts``, which VS Code accepts and
    parses -- it read ``text`` to title the conversation -- but the bubble it
    renders is built from ``parts``, so every question came out blank while
    every answer was there. Measured afterwards on the real store: of eleven
    requests VS Code wrote itself, eleven carry exactly one part; of seventeen
    Ferry wrote, all seventeen carried none.

    The part is the same text twice over, once as characters and once as editor
    coordinates, both derived from the text rather than invented.
    """
    lines = text.splitlines() or [""]
    part = {
        "kind": "text",
        "text": text,
        "range": {"start": 0, "endExclusive": len(text)},
        "editorRange": {
            "startLineNumber": 1,
            "startColumn": 1,
            "endLineNumber": len(lines),
            "endColumn": len(lines[-1]) + 1,
        },
    }
    return {"text": text, "parts": [part] if text else []}


def _ids(conversation_id: UUID, ordinal: int) -> dict[str, str]:
    """Stable request and response ids for one exchange.

    Derived from the conversation id rather than randomly, so re-importing the
    same conversation produces the same ids instead of a fresh set each time.
    """
    seed = f"{conversation_id}:{ordinal}"
    return {
        "requestId": f"request_{uuid5(_REQUEST_NAMESPACE, seed)}",
        "responseId": f"response_{uuid5(_REQUEST_NAMESPACE, 'r' + seed)}",
    }


def _exchanges(conversation: Conversation) -> list[dict[str, Any]]:
    """UCS messages regrouped as Copilot's question-and-answer pairs.

    UCS is a flat ordered list; a Copilot document is a list of *requests*,
    each one question with everything that answered it. An assistant message
    arriving before any question opens a request with an empty one rather than
    being dropped, because a conversation that starts with an answer is odd but
    it is still the person's conversation.
    """
    tool = conversation.source_tool
    requests: list[dict[str, Any]] = []
    answers: list[str] = []

    def close() -> None:
        if requests:
            body = "\n\n".join(part for part in answers if part.strip())
            requests[-1]["response"] = [{"value": body}] if body else []

    for message in conversation.messages:
        pieces = [_rendered(block, tool) for block in message.content]
        if message.role == "user":
            close()
            answers = []
            requests.append(
                {
                    **_ids(conversation.id, len(requests)),
                    "message": _message("\n\n".join(p for p in pieces if p.strip())),
                    "response": [],
                }
            )
        else:
            if not requests:
                requests.append(
                    {
                        **_ids(conversation.id, 0),
                        "message": _message(""),
                        "response": [],
                    }
                )
            answers.extend(pieces)
    close()
    return requests


def synthesize_document(conversation: Conversation) -> dict[str, Any]:
    """Build a VS Code chat document for a conversation from another tool.

    **Every field here was measured, and the shape is the smallest one proven
    to work** (PROGRESS #203, #204). A real document carries 24 fields per
    request, including the Copilot extension's own manifest, token counts,
    credits spent and model state. Ferry writes none of that: it would be
    inventing telemetry about a conversation that never happened in VS Code,
    and the experiment showed VS Code neither needs nor asks for it.

    What is written is the document proven to render, plus ``customTitle``,
    which appears in real documents and carries the conversation's real name
    instead of letting VS Code fall back to the first message.

    ``responderUsername`` deliberately does **not** say "GitHub Copilot". The
    measured values are that and the empty string, and it is free text; naming
    the tool that actually answered keeps Ferry's rule that a converted
    conversation is never presented as native.
    """
    return {
        "version": 3,
        "sessionId": str(conversation.id),
        "creationDate": _epoch_ms(conversation.created_at),
        "requesterUsername": "",
        "responderUsername": conversation.source_tool,
        "customTitle": conversation.title or "Imported conversation",
        "requests": _exchanges(conversation),
    }
