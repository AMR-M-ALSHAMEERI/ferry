"""Turn one Codex rollout into a UCS conversation, streaming.

Every record is uniformly ``{type, timestamp, payload}`` -- far more regular
than Claude Code's transcript -- but the format carries **every turn twice**,
and that is the thing to get right:

- ``response_item`` is the canonical record. Messages, tool calls and tool
  outputs are read from here.
- ``event_msg`` is the interface's echo of the same turn. Reading both
  double-counts the entire conversation.

Two exceptions justify reaching into ``event_msg`` anyway:

**Thinking.** ``response_item``'s ``reasoning`` payload holds
``encrypted_content`` and no plaintext at all; the readable text lives only in
``event_msg``/``agent_reasoning``. Mapping the canonical one would have
produced reasoning blocks full of ciphertext with a perfectly plausible block
count -- the same trap as Claude Code storing thinking under ``thinking``.

**MCP tool results.** Of 461 ``mcp_tool_call_end`` records on the probed
machine, **only 206 had a matching call id in ``response_item``**. The other
255 exist nowhere else, so skipping ``event_msg`` wholesale would silently drop
real tool output. They are read, then de-duplicated by call id against what
``response_item`` already provided.

The file is read a line at a time and never loaded whole: a single rollout
reached 53 MB on the probe machine, and third-party reports describe files an
order of magnitude larger.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from ferry.adapters.pathutil import basename
from ferry.ucs import (
    Attachment,
    ContentBlock,
    Conversation,
    ImageBlock,
    Message,
    Role,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Workspace,
)

__all__ = [
    "CALL_PAYLOADS",
    "OUTPUT_PAYLOADS",
    "PendingAttachment",
    "SessionRead",
    "read_rollout",
]

CALL_PAYLOADS = frozenset({"function_call", "custom_tool_call"})
OUTPUT_PAYLOADS = frozenset({"function_call_output", "custom_tool_call_output"})
_TEXT_BLOCKS = {"input_text", "output_text", "text", "summary_text"}
_ATTACHMENT_NAMESPACE = UUID("6ba7b813-9dad-11d1-80b4-00c04fd430c8")
_EXTENSION_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


@dataclass(frozen=True)
class PendingAttachment:
    """An image decoded out of the transcript, not yet written anywhere."""

    record: Attachment
    data: bytes


@dataclass
class SessionRead:
    """Everything one rollout yielded."""

    conversation: Conversation | None
    attachments: list[PendingAttachment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    bytes_read: int = 0


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _decode_data_uri(uri: str) -> tuple[str, bytes] | None:
    """``data:image/png;base64,...`` into a mime type and its bytes.

    Codex also records ``local_images`` paths beside these, but every one of
    them pointed at a ``%TEMP%`` clipboard file that no longer existed. The URI
    is the only surviving copy, so it is the only one trusted here.
    """
    if not uri.startswith("data:"):
        return None
    header, _, encoded = uri.partition(",")
    if not encoded or ";base64" not in header:
        return None
    mime = header[len("data:") :].split(";", 1)[0] or "application/octet-stream"
    try:
        return mime, base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None


def _image(conversation_id: UUID, ordinal: int, uri: str) -> PendingAttachment | None:
    decoded = _decode_data_uri(uri)
    if decoded is None:
        return None
    mime, data = decoded
    attachment_id = uuid5(_ATTACHMENT_NAMESPACE, f"{conversation_id}:image:{ordinal}")
    extension = _EXTENSION_BY_MIME.get(mime) or mimetypes.guess_extension(mime) or ".bin"
    return PendingAttachment(
        record=Attachment(
            id=attachment_id,
            filename=f"image-{ordinal}{extension}",
            mime_type=mime,
            bundle_path=f"attachments/{conversation_id}/{attachment_id}{extension}",
            sha256=hashlib.sha256(data).hexdigest(),
        ),
        data=data,
    )


def _message_blocks(
    payload: dict[str, Any], out: SessionRead, conversation_id: UUID
) -> list[ContentBlock]:
    """Map ``response_item``/``message`` content onto UCS blocks."""
    raw = payload.get("content")
    if isinstance(raw, str):
        return [TextBlock(text=raw)]
    if not isinstance(raw, list):
        out.warnings.append(f"message content was {type(raw).__name__}; skipped")
        return []

    blocks: list[ContentBlock] = []
    for item in raw:
        if not isinstance(item, dict):
            out.warnings.append(f"content block was {type(item).__name__}; skipped")
            continue
        kind = item.get("type")
        if kind in _TEXT_BLOCKS:
            blocks.append(TextBlock(text=str(item.get("text", ""))))
        elif kind == "input_image":
            uri = item.get("image_url")
            pending = (
                _image(conversation_id, len(out.attachments), uri) if isinstance(uri, str) else None
            )
            if pending is None:
                out.warnings.append("image could not be decoded; kept only in source_raw")
                continue
            out.attachments.append(pending)
            blocks.append(ImageBlock(attachment_id=pending.record.id))
        else:
            out.warnings.append(f"unknown content block type {kind!r}; skipped")
    return blocks


_ROLES: dict[str, Role] = {
    "user": "user",
    "assistant": "assistant",
    "system": "system",
    "tool": "tool",
}


def _role_of(payload: dict[str, Any]) -> Role:
    """UCS role for a message, defaulting to ``user`` for anything unrecognised."""
    role = payload.get("role")
    return _ROLES.get(role, "user") if isinstance(role, str) else "user"


def read_rollout(path: Path, *, warn_over_bytes: int | None = None) -> SessionRead:
    """Read one rollout transcript.

    Args:
        path: ``sessions/YYYY/MM/DD/rollout-<stamp>-<uuid>.jsonl``.
        warn_over_bytes: Emit a note when the file exceeds this. Does not
            change behaviour -- the read streams either way.

    Returns:
        A :class:`SessionRead`. ``conversation`` is ``None`` when the file held
        no messages, which is a skip rather than an error.
    """
    out = SessionRead(conversation=None)

    from ferry.adapters.codex.paths import thread_id_of

    conversation_id = thread_id_of(path)
    if conversation_id is None:
        out.warnings.append(f"{path.name}: not a rollout filename; skipped")
        return out

    size = path.stat().st_size
    out.bytes_read = size
    if warn_over_bytes is not None and size > warn_over_bytes:
        out.notes.append(f"{path.name}: {size:,} bytes")

    messages: list[Message] = []
    stamps: list[datetime] = []
    cwd: str | None = None
    version: str | None = None
    provider: str | None = None
    title: str | None = None
    header: dict[str, Any] | None = None
    encrypted_reasoning = 0

    # De-duplicating MCP results needs both sides of the file, and the two can
    # appear in either order, so the decision is deferred to the end rather
    # than guessed as we go.
    response_call_ids: set[str] = set()
    mcp_positions: list[tuple[int, str]] = []

    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                out.warnings.append(f"{path.name}:{number}: unreadable JSON ({exc.msg}); skipped")
                continue
            if not isinstance(record, dict):
                out.warnings.append(f"{path.name}:{number}: line is not an object; skipped")
                continue

            stamp = _timestamp(record.get("timestamp"))
            if stamp is not None:
                stamps.append(stamp)
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            record_type = record.get("type")

            # Four record types carry structured config instead of a typed
            # event, so `payload.type` is simply absent. Branching on it first
            # would lose every one of them.
            if record_type == "session_meta":
                # Kept verbatim, once. Codex validates this record strictly --
                # `base_instructions` and `context_window` are dicts, not
                # scalars, and a wrong shape makes it reject the entire file
                # with "does not start with session metadata". Carrying the
                # real one is ~40 KB and removes any need to guess the schema
                # when writing the conversation back.
                if header is None:
                    header = payload
                cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else cwd
                if isinstance(payload.get("cli_version"), str):
                    version = payload["cli_version"]
                if isinstance(payload.get("model_provider"), str):
                    provider = payload["model_provider"]
                continue
            if record_type not in {"response_item", "event_msg"}:
                continue

            kind = payload.get("type")

            if record_type == "response_item":
                if kind == "message":
                    blocks = _message_blocks(payload, out, conversation_id)
                    if blocks:
                        messages.append(
                            Message(role=_role_of(payload), content=blocks, timestamp=stamp)
                        )
                elif kind in CALL_PAYLOADS:
                    call_id = payload.get("call_id") or payload.get("id")
                    arguments = payload.get("arguments") or payload.get("input")
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            arguments = {"raw": arguments}
                    messages.append(
                        Message(
                            role="assistant",
                            content=[
                                ToolUseBlock(
                                    name=str(payload.get("name") or kind),
                                    input=arguments if isinstance(arguments, dict) else {},
                                    id=str(call_id) if call_id else None,
                                )
                            ],
                            timestamp=stamp,
                        )
                    )
                elif kind in OUTPUT_PAYLOADS:
                    call_id = payload.get("call_id") or payload.get("id")
                    if call_id:
                        response_call_ids.add(str(call_id))
                    messages.append(
                        Message(
                            role="tool",
                            content=[
                                ToolResultBlock(
                                    tool_use_id=str(call_id) if call_id else "",
                                    output=payload.get("output"),
                                )
                            ],
                            timestamp=stamp,
                        )
                    )
                elif kind == "reasoning":
                    # `encrypted_content`, no plaintext. Nothing to carry.
                    encrypted_reasoning += 1
                continue

            # event_msg: only the two things response_item cannot give us.
            if kind == "agent_reasoning":
                text = payload.get("text")
                if isinstance(text, str) and text:
                    messages.append(
                        Message(
                            role="assistant",
                            content=[ThinkingBlock(text=text)],
                            timestamp=stamp,
                        )
                    )
            elif kind == "mcp_tool_call_end":
                call_id = payload.get("call_id")
                mcp_positions.append((len(messages), str(call_id) if call_id else ""))
                messages.append(
                    Message(
                        role="tool",
                        content=[
                            ToolResultBlock(
                                tool_use_id=str(call_id) if call_id else "",
                                output=payload.get("result"),
                            )
                        ],
                        timestamp=stamp,
                    )
                )
            elif kind == "user_message" and not any(m.role == "user" for m in messages[-3:]):
                # Only reached when response_item did not carry the turn; the
                # canonical record is preferred whenever it exists.
                text = payload.get("message")
                if isinstance(text, str) and text:
                    messages.append(
                        Message(role="user", content=[TextBlock(text=text)], timestamp=stamp)
                    )

    duplicates = [index for index, call_id in mcp_positions if call_id in response_call_ids]
    for index in sorted(duplicates, reverse=True):
        del messages[index]
    if duplicates:
        out.notes.append(
            f"{len(duplicates)} MCP results already present as response_item output; de-duplicated"
        )
    unique_mcp = len(mcp_positions) - len(duplicates)
    if unique_mcp:
        out.notes.append(f"{unique_mcp} MCP results existed only in the event stream; kept")
    if encrypted_reasoning:
        out.notes.append(
            f"{encrypted_reasoning} reasoning records are encrypted and carry no text; "
            "readable thinking came from agent_reasoning"
        )

    if not messages:
        out.notes.append(f"{path.name}: no messages; skipped")
        return out

    # Dated by its messages, not by every record in the file. The max over all
    # records is the last token-count or window event, which a rebuilt rollout
    # does not reproduce -- so it would drift on every round trip while
    # describing something the user never sees. "When the conversation was last
    # spoken in" is both stabler and closer to what the field means.
    message_stamps = [m.timestamp for m in messages if m.timestamp is not None]
    stamps = message_stamps or stamps
    if not stamps:
        out.warnings.append(f"{path.name}: no timestamps anywhere; skipped")
        return out

    for message in messages:
        for block in message.content:
            if block.type == "text" and not title:
                title = block.text.strip().splitlines()[0][:80] if block.text.strip() else None
        if title:
            break

    raw: dict[str, Any] = {"format": "codex-rollout-jsonl"}
    if provider:
        raw["model_provider"] = provider
    if header is not None:
        raw["session_meta"] = header

    out.conversation = Conversation(
        id=conversation_id,
        source_tool="codex",
        source_tool_version=version,
        source_id=str(conversation_id),
        title=title,
        created_at=min(stamps),
        updated_at=max(stamps),
        workspace=Workspace(
            name=basename(cwd) if cwd else None,
            original_path=cwd,
        ),
        messages=messages,
        attachments=[pending.record for pending in out.attachments],
        source_raw=raw,
    )
    return out
