"""Write a UCS conversation back out as a Codex rollout.

Unlike the Claude Code adapter, this one always **rebuilds** — there is no
verbatim replay path, and that is a deliberate choice rather than a missing
feature. A single rollout reached 53 MB on the probe machine and six
conversations totalled 121 MB, of which roughly a third is the interface's own
echo of turns the canonical records already hold. Copying all of it into every
bundle to preserve token counters and window bookkeeping is a poor trade.

What *is* copied verbatim is the header. Codex validates ``session_meta``
strictly: ``base_instructions`` and ``context_window` are objects rather than
scalars, and a wrong shape makes it reject the whole file with "does not start
with session metadata" — the conversation vanishes, no error, no partial read.
Carrying the real header costs about 40 KB per conversation and removes any
need to guess that schema. Everything below it is rebuilt from UCS.

Verified end to end: a rollout written this way was accepted by Codex's own
migration scanner ("1 eligible, 0 failed") and resumed by
``codex exec resume <id>``, which printed the session id and replayed the turn.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from ferry.ucs import Attachment, Conversation, Message

__all__ = [
    "REBUILD_NOTES",
    "Rebuild",
    "rollout_lines",
    "rollout_stamp",
    "session_meta_for",
]

REBUILD_NOTES = (
    "records rebuilt from UCS: token counts, window state, turn context and "
    "compaction history are not carried",
    "encrypted reasoning is not restored: it holds no readable text and cannot "
    "be reconstructed from the thinking that was kept",
)
"""What a rebuilt rollout does not contain. Reported, never absorbed."""


@dataclass
class Rebuild:
    """Where the conversation is being written to."""

    cwd: str
    thread_id: UUID
    images: dict[UUID, tuple[Attachment, bytes]] = field(default_factory=dict)


def rollout_stamp(moment: datetime) -> str:
    """``YYYY-MM-DDTHH-MM-SS`` — ISO with the colons removed, as the filename needs."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H-%M-%S")


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def session_meta_for(conversation: Conversation, rebuild: Rebuild) -> dict[str, Any] | None:
    """The header record, taken from the bundle and re-pointed at this machine.

    Returns ``None`` when the bundle carries no header — a conversation that
    came from another tool, or from a Ferry old enough not to have kept one.
    The caller must treat that as "cannot write a valid rollout" rather than
    inventing a header, because an invalid one is rejected silently.
    """
    raw = conversation.source_raw or {}
    header = raw.get("session_meta")
    if not isinstance(header, dict):
        return None
    payload = dict(header)
    previous = payload.get("id")
    payload["id"] = str(rebuild.thread_id)
    # `session_id` is NOT a second copy of `id`. On a subagent thread it holds
    # the *parent* thread's id -- the one case in the probed data where the two
    # differ, and overwriting it reparented the subagent to itself. It is only
    # updated when it was genuinely the same identity being renamed.
    if payload.get("session_id") == previous:
        payload["session_id"] = str(rebuild.thread_id)
    payload["cwd"] = rebuild.cwd
    # The header's own timestamp is left alone. It records when the session
    # began, which importing does not change; rewriting it made source_raw
    # differ across a round trip that had moved nothing.
    return payload


def _content_blocks(message: Message, rebuild: Rebuild) -> list[dict[str, Any]]:
    """UCS blocks as Codex message content.

    The text block type depends on direction: what the user sent is
    ``input_text``, what the model produced is ``output_text``. Emitting the
    wrong one produces a file that parses and renders incorrectly.
    """
    text_type = "output_text" if message.role == "assistant" else "input_text"
    blocks: list[dict[str, Any]] = []
    for block in message.content:
        if block.type == "text":
            blocks.append({"type": text_type, "text": block.text})
        elif block.type == "image":
            found = rebuild.images.get(block.attachment_id)
            if found is None:
                continue
            attachment, data = found
            encoded = base64.b64encode(data).decode("ascii")
            blocks.append(
                {
                    "type": "input_image",
                    "detail": "high",
                    "image_url": f"data:{attachment.mime_type};base64,{encoded}",
                }
            )
    return blocks


def _records_for(message: Message, rebuild: Rebuild, ordinal: int) -> list[dict[str, Any]]:
    """One UCS message as the Codex records it came from."""
    stamp = _iso(message.timestamp) if message.timestamp else None
    out: list[dict[str, Any]] = []

    def emit(record_type: str, payload: dict[str, Any]) -> None:
        record: dict[str, Any] = {"type": record_type, "payload": payload}
        if stamp:
            record["timestamp"] = stamp
        out.append(record)

    content = _content_blocks(message, rebuild)
    if content:
        emit(
            "response_item",
            {
                "type": "message",
                "id": f"msg_ferry_{ordinal}",
                "role": message.role if message.role in {"user", "assistant"} else "user",
                "content": content,
            },
        )

    for block in message.content:
        if block.type == "thinking":
            # Back to event_msg, which is where it was read from. response_item
            # reasoning is encrypted and cannot be reconstructed.
            emit("event_msg", {"type": "agent_reasoning", "text": block.text})
        elif block.type == "tool_use":
            emit(
                "response_item",
                {
                    "type": "function_call",
                    "id": f"fc_ferry_{ordinal}",
                    "call_id": block.id or f"call_ferry_{ordinal}",
                    "name": block.name,
                    "arguments": json.dumps(block.input, ensure_ascii=False),
                },
            )
        elif block.type == "tool_result":
            emit(
                "response_item",
                {
                    "type": "function_call_output",
                    "id": f"fco_ferry_{ordinal}",
                    "call_id": block.tool_use_id or f"call_ferry_{ordinal}",
                    "output": block.output,
                },
            )
    return out


def rollout_lines(conversation: Conversation, rebuild: Rebuild) -> bytes | None:
    """The whole rollout as JSONL, or ``None`` if no header was available."""
    header = session_meta_for(conversation, rebuild)
    if header is None:
        return None

    records: list[dict[str, Any]] = [
        {
            "type": "session_meta",
            "timestamp": _iso(conversation.created_at),
            "payload": header,
        }
    ]
    for ordinal, message in enumerate(conversation.messages):
        records.extend(_records_for(message, rebuild, ordinal))

    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode(
        "utf-8"
    )
