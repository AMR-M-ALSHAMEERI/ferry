"""Turning a replayed Copilot session into a UCS conversation.

The delta replay happens in :mod:`ferry.adapters.copilot.deltas`; by the time
anything here runs there is an ordinary document to read. What is left is the
mapping, and it has two rules that are not obvious from the data:

**Text is recognised by what it lacks.** Copilot tags most response blocks with
a ``kind`` -- ``thinking``, ``toolInvocationSerialized``, ``mcpServersStarting``
-- but the assistant's actual prose is an untagged ``MarkdownString``. Three
different key sets for it appear in 203 real blocks, differing only in which
support flags VS Code felt like including. So a block is text when it has a
``value`` and no ``kind``, never by matching a key set.

**Not every block is conversation.** ``mcpServersStarting`` and
``autoModeResolution`` are the interface narrating itself. They are counted and
dropped rather than turned into messages nobody wrote.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from ferry.adapters.copilot import paths as cp_paths
from ferry.adapters.copilot.deltas import replay_lines
from ferry.ucs import (
    Attachment,
    Conversation,
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Workspace,
)
from ferry.ucs.models import ContentBlock

__all__ = ["NARRATION_KINDS", "PendingImage", "SessionRead", "read_session"]

NARRATION_KINDS = frozenset({"mcpServersStarting", "autoModeResolution"})
"""Blocks that are the interface talking about itself, not the conversation.

Dropped, but counted -- ``codeCitations`` and the like may join this list, and
a silent drop is how a real block type gets lost for a release.
"""

_TITLE_LIMIT = 80


@dataclass
class PendingImage:
    """An image decoded out of a transcript, waiting to be written."""

    record: Attachment
    data: bytes


@dataclass
class SessionRead:
    """What one session file yielded."""

    conversation: Conversation | None = None
    images: list[PendingImage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    dropped_kinds: dict[str, int] = field(default_factory=dict)


def _when(value: Any) -> datetime | None:
    """A VS Code epoch-millisecond timestamp as a datetime."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _markdown(block: Any) -> str | None:
    """The text of an untagged ``MarkdownString`` block, if that is what it is."""
    if not isinstance(block, dict) or "kind" in block:
        return None
    value = block.get("value")
    return value if isinstance(value, str) else None


def _thinking_text(value: Any) -> str:
    """The text of a thinking block, whatever shape it arrived in.

    Half the thinking blocks on the sample carry a string and half carry a
    list, and **on this machine every one of them was empty** -- Copilot writes
    the marker without the reasoning. Both shapes are handled anyway: assuming
    a string would silently drop the text on any build that starts storing it,
    and that is a change no test of ours would notice.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(part for part in value if isinstance(part, str))
    return ""


def _tool_blocks(block: dict[str, Any]) -> list[ContentBlock]:
    """A tool invocation as a call, and its result when one is recorded.

    ``toolCallId`` links the two. Copilot records the result inside the same
    block rather than as a separate one, so both come from here -- and a call
    that never completed yields only the call, which is what happened.
    """
    call_id = block.get("toolCallId")
    name = block.get("toolId")
    specific = block.get("toolSpecificData")
    out: list[ContentBlock] = [
        ToolUseBlock(
            name=name if isinstance(name, str) and name else "unknown",
            id=call_id if isinstance(call_id, str) else None,
            input=specific if isinstance(specific, dict) else {},
        )
    ]
    details = block.get("resultDetails")
    if details is not None and isinstance(call_id, str):
        out.append(ToolResultBlock(tool_use_id=call_id, output=details))
    return out


def _response_blocks(
    response: Any, dropped: dict[str, int]
) -> tuple[list[ContentBlock], list[str]]:
    """Map one turn's response list into UCS content blocks."""
    blocks: list[ContentBlock] = []
    notes: list[str] = []
    if not isinstance(response, list):
        return blocks, notes

    for raw in response:
        text = _markdown(raw)
        if text is not None:
            if text:
                blocks.append(TextBlock(text=text))
            continue
        if not isinstance(raw, dict):
            dropped["not-an-object"] = dropped.get("not-an-object", 0) + 1
            continue

        kind = raw.get("kind")
        if kind == "thinking":
            value = _thinking_text(raw.get("value"))
            if value:
                blocks.append(ThinkingBlock(text=value))
            else:
                # Every one of the 36 thinking blocks on the sample was empty.
                # Copilot writes the marker and not the reasoning, the same way
                # Codex records reasoning it will not decrypt. Counted, because
                # a block that exists and holds nothing is worth saying out
                # loud rather than quietly discarding.
                dropped["thinking (no text stored)"] = (
                    dropped.get("thinking (no text stored)", 0) + 1
                )
            continue
        if kind == "toolInvocationSerialized":
            blocks.extend(_tool_blocks(raw))
            continue
        if kind == "inlineReference":
            # A file or symbol named inside the answer's prose. Dropping it
            # takes a word out of the middle of a sentence, so the name is
            # kept as text; the link it carried is not representable in UCS.
            name = raw.get("name")
            if isinstance(name, str) and name:
                blocks.append(TextBlock(text=name))
            continue
        if isinstance(kind, str):
            dropped[kind] = dropped.get(kind, 0) + 1
        else:
            dropped["no-kind-no-value"] = dropped.get("no-kind-no-value", 0) + 1

    return blocks, notes


def _images(turn: dict[str, Any], conversation_id: UUID) -> list[PendingImage]:
    """Images pasted into a turn.

    Copilot stores these **inside the transcript** as base64, not only in the
    shared ``vscode-chat-images`` directory, so the bytes travel with the
    conversation and no second file has to be found.
    """
    found: list[PendingImage] = []
    variables = (turn.get("variableData") or {}).get("variables")
    if not isinstance(variables, list):
        return found

    for variable in variables:
        if not isinstance(variable, dict) or variable.get("kind") != "image":
            continue
        value = variable.get("value")
        encoded = value.get("$base64") if isinstance(value, dict) else None
        if not isinstance(encoded, str):
            continue
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            continue
        mime = variable.get("mimeType")
        mime = mime if isinstance(mime, str) and mime else "image/png"
        name = variable.get("name")
        attachment_id = uuid4()
        suffix = mime.rsplit("/", 1)[-1] or "png"
        found.append(
            PendingImage(
                record=Attachment(
                    id=attachment_id,
                    filename=name if isinstance(name, str) and name else f"image.{suffix}",
                    mime_type=mime,
                    bundle_path=f"attachments/{conversation_id}/{attachment_id}.{suffix}",
                    sha256=hashlib.sha256(data).hexdigest(),
                ),
                data=data,
            )
        )
    return found


def _title(document: dict[str, Any], messages: list[Message]) -> str | None:
    """The conversation's name.

    ``customTitle`` when the user set one. Otherwise the first thing they
    actually typed, trimmed -- Copilot itself shows a generated summary that is
    not stored, so there is nothing better available and a conversation with no
    name at all is worse than a truncated one.
    """
    custom = document.get("customTitle")
    if isinstance(custom, str) and custom.strip():
        return custom.strip()
    for message in messages:
        if message.role != "user":
            continue
        for block in message.content:
            if isinstance(block, TextBlock) and block.text.strip():
                text = " ".join(block.text.split())
                return text if len(text) <= _TITLE_LIMIT else text[: _TITLE_LIMIT - 3] + "..."
    return None


def _workspace_name(original: str, *, is_file: bool) -> str | None:
    """A readable name for the place a conversation happened.

    A folder is named by its last segment. A ``.code-workspace`` file is named
    by the file, minus the extension -- except for an *untitled* workspace,
    which VS Code stores as ``Workspaces/<id>/workspace.json``, where the
    filename says nothing and the id at least identifies it.
    """
    from ferry.adapters.pathutil import basename

    leaf = basename(original)
    if not leaf:
        return None
    if not is_file:
        return leaf
    if leaf == "workspace.json":
        parent = basename(original[: len(original) - len(leaf)].rstrip("/\\"))
        return parent or leaf
    return leaf.removesuffix(".code-workspace")


def _workspace(key: str, env: Any = None) -> Workspace:
    """What is known about where the conversation happened.

    ``path_hash`` is the ``workspaceStorage`` key, which is a digest of the
    folder path and its creation time -- so it identifies the workspace without
    disclosing where it was. An empty-window conversation has no workspace and
    says so rather than inventing one.
    """
    if not key:
        return Workspace(name=None, original_path=None, path_hash=None)

    original: str | None = None
    name: str | None = None
    marker = cp_paths.workspace_storage(env) / key / "workspace.json"
    if marker.is_file():
        try:
            import json

            meta = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        uri = meta.get("folder") or meta.get("workspace")
        if isinstance(uri, str):
            from urllib.parse import unquote

            original = unquote(uri.removeprefix("file:///"))
            name = _workspace_name(original, is_file="folder" not in meta)
    return Workspace(name=name, original_path=original, path_hash=key)


def read_session(path: Path, key: str = "", env: Any = None) -> SessionRead:
    """Read one Copilot session file into a UCS conversation.

    Args:
        path: The ``<uuid>.jsonl`` transcript.
        key: The ``workspaceStorage`` directory name, or ``""`` for an
            empty-window conversation.
        env: Environment override, for locating ``workspace.json``.

    Returns:
        The conversation and its images, or a :class:`SessionRead` whose
        ``conversation`` is ``None`` when the file holds no turns.
    """
    found = SessionRead()

    conversation_id = cp_paths.session_id_of(path)
    if conversation_id is None:
        found.notes.append("filename is not a conversation id")
        return found

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        found.notes.append(f"unreadable: {exc}")
        return found

    replayed = replay_lines(lines)
    document = replayed.document
    if replayed.unknown_kinds:
        listed = ", ".join(f"kind {k} x{n}" for k, n in sorted(replayed.unknown_kinds.items()))
        found.warnings.append(f"{path.name}: unrecognised delta records skipped ({listed})")
    if replayed.unparseable:
        found.warnings.append(f"{path.name}: {replayed.unparseable} unreadable lines skipped")
    if replayed.bad_paths:
        found.warnings.append(f"{path.name}: {replayed.bad_paths} deltas did not apply")

    turns = document.get("requests")
    turns = turns if isinstance(turns, list) else []

    messages: list[Message] = []
    hidden = 0
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        if turn.get("hiddenFromTranscript") is True:
            # Copilot hides a turn from its own display. Carrying it would put
            # text in the transcript that the person never saw in it.
            hidden += 1
            continue

        asked = turn.get("message")
        typed = asked.get("text") if isinstance(asked, dict) else None
        images = _images(turn, conversation_id)
        found.images.extend(images)

        user_content: list[ContentBlock] = []
        if isinstance(typed, str) and typed.strip():
            user_content.append(TextBlock(text=typed))
        user_content.extend(ImageBlock(attachment_id=image.record.id) for image in images)
        if user_content:
            messages.append(
                Message(role="user", content=user_content, timestamp=_when(turn.get("timestamp")))
            )

        blocks, notes = _response_blocks(turn.get("response"), found.dropped_kinds)
        found.notes.extend(notes)
        if blocks:
            model = turn.get("modelId")
            messages.append(
                Message(
                    role="assistant",
                    content=blocks,
                    timestamp=_when(turn.get("responseTimestamp")),
                    model=model if isinstance(model, str) and model else None,
                )
            )

    if hidden:
        found.notes.append(f"{hidden} turns hidden from the transcript by Copilot were not carried")
    if found.dropped_kinds:
        listed = ", ".join(f"{k} x{n}" for k, n in sorted(found.dropped_kinds.items()))
        found.notes.append(f"interface-only blocks not carried: {listed}")

    if not messages:
        found.notes.append("no messages")
        return found

    created = _when(document.get("creationDate")) or datetime.now(UTC)
    stamps = [m.timestamp for m in messages if m.timestamp is not None]
    session_id = document.get("sessionId")

    found.conversation = Conversation(
        id=conversation_id,
        source_tool="copilot",
        source_id=session_id if isinstance(session_id, str) else str(conversation_id),
        title=_title(document, messages),
        created_at=created,
        updated_at=max(stamps) if stamps else created,
        workspace=_workspace(key, env),
        messages=messages,
        attachments=[image.record for image in found.images],
        source_raw={"document": document, "workspace_key": key},
    )
    return found
