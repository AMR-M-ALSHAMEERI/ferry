"""Write conversations back into Claude Code's storage.

Two routes in, and the difference matters:

**Replay** -- when the bundle carries the original JSONL under ``source_raw``,
every line goes back exactly as it came out, with only the machine-specific
fields rewritten. Nothing is re-derived, so nothing can be lost in the
re-derivation. This is the path a same-machine or same-tool migration takes and
it is the reason ``source_raw`` exists.

**Synthesis** -- when there is no original (a bundle written without
``source_raw``, or a conversation that came from another tool), the records are
built from UCS. That is lossy in ways UCS cannot express: ``tool_use`` has no id
in UCS, so the ids linking a call to its result have to be regenerated, and the
regenerated pair is internally consistent but is not the original id. Every such
loss is reported, never absorbed.

Only *structural* fields are rewritten on the way in -- ``cwd`` and the absolute
pointers to spilled tool output. Message content is never touched: PLAN.md
section 3.2 says Ferry does not edit what the user wrote, and a path that
appears inside a message is something the user or the assistant said, not a
reference the tool will follow.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from ferry.ucs import Conversation, Message

__all__ = [
    "SYNTHESIS_NOTES",
    "remap_prefix",
    "remap_record",
    "session_lines",
    "synthesize_records",
]

_RECORD_NAMESPACE = UUID("6ba7b812-9dad-11d1-80b4-00c04fd430c8")

SYNTHESIS_NOTES = (
    "records rebuilt from UCS: original non-message records (titles, modes, "
    "hook summaries, context injections) are not present",
    "tool_use ids regenerated: UCS does not carry them, so calls and results "
    "are linked consistently but not with the original identifiers",
)
"""What is unavoidably lost when a session is rebuilt rather than replayed."""


@dataclass
class Remap:
    """How one machine's paths become another's.

    Args:
        old_cwd: The working directory recorded in the bundle, or ``None`` when
            the source never stored one.
        new_cwd: Where the conversation should live on this machine.
        old_sidecar_root: Directory the source machine spilled tool output into.
        new_sidecar_root: Where those files now live.
    """

    old_cwd: str | None
    new_cwd: str
    old_sidecar_root: str | None = None
    new_sidecar_root: str | None = None
    rewrites: int = field(default=0)


def remap_prefix(path_text: str, rules: tuple[tuple[str, str], ...]) -> str:
    """Apply the first matching prefix rule to an absolute path.

    Comparison is case-insensitive and separator-insensitive, because the same
    directory is spelled ``C:\\Users\\a``, ``C:/Users/a`` and ``c:\\users\\a``
    in different records of the same transcript. The replacement keeps the
    separator style of the rule's target, not of the original.
    """

    def key(text: str) -> str:
        return text.replace("\\", "/").rstrip("/").lower()

    candidate = key(path_text)
    for old, new in rules:
        old_key = key(old)
        if candidate == old_key:
            return new
        if candidate.startswith(old_key + "/"):
            return new.rstrip("/\\") + path_text[len(old) :]
    return path_text


def remap_record(record: dict[str, Any], remap: Remap) -> dict[str, Any]:
    """Rewrite the machine-specific fields of one transcript record.

    Returns a new dict; the input is left alone so a failed import cannot have
    half-mutated the caller's data.
    """
    out = dict(record)
    if isinstance(out.get("cwd"), str):
        out["cwd"] = remap.new_cwd
        remap.rewrites += 1
    result = out.get("toolUseResult")
    if isinstance(result, dict) and isinstance(result.get("persistedOutputPath"), str):
        # Rebuilt from the filename rather than string-substituted: the new
        # location is known exactly, and substitution would depend on the old
        # path being spelled the way we expect it to be.
        if remap.new_sidecar_root:
            moved = dict(result)
            name = Path(str(result["persistedOutputPath"])).name
            moved["persistedOutputPath"] = str(Path(remap.new_sidecar_root) / name)
            out["toolUseResult"] = moved
            remap.rewrites += 1
    return out


def _tool_use_id(conversation_id: UUID, ordinal: int) -> str:
    """A stable stand-in id for a tool call UCS did not preserve."""
    digest = hashlib.sha256(f"{conversation_id}:{ordinal}".encode()).hexdigest()
    return f"toolu_{digest[:24]}"


def _blocks_for(message: Message, conversation_id: UUID, counter: list[int]) -> list[Any]:
    blocks: list[Any] = []
    for block in message.content:
        if block.type == "text":
            blocks.append({"type": "text", "text": block.text})
        elif block.type == "thinking":
            blocks.append(
                {
                    "type": "thinking",
                    "thinking": block.text,
                    "signature": block.signature or "",
                }
            )
        elif block.type == "tool_use":
            counter[0] += 1
            blocks.append(
                {
                    "type": "tool_use",
                    "id": _tool_use_id(conversation_id, counter[0]),
                    "name": block.name,
                    "input": block.input,
                }
            )
        elif block.type == "tool_result":
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": block.output,
                }
            )
    return blocks


def synthesize_records(
    conversation: Conversation, *, cwd: str, version: str
) -> list[dict[str, Any]]:
    """Build transcript records from UCS alone.

    The threading is rebuilt as a straight chain -- ``parentUuid`` pointing at
    the previous record -- because UCS stores messages as an ordered list and
    does not carry the original tree. A branched conversation therefore comes
    back linear. That is a real loss and it is named in :data:`SYNTHESIS_NOTES`
    rather than hidden.
    """
    records: list[dict[str, Any]] = []
    parent: str | None = None
    counter = [0]
    for index, message in enumerate(conversation.messages):
        record_uuid = str(uuid5(_RECORD_NAMESPACE, f"{conversation.id}:{index}"))
        stamp = message.timestamp or conversation.created_at
        role = message.role
        payload: dict[str, Any] = {
            "role": "assistant" if role == "assistant" else "user",
            "content": _blocks_for(message, conversation.id, counter),
        }
        if message.model:
            payload["model"] = message.model
        record: dict[str, Any] = {
            "type": "assistant" if role == "assistant" else "user",
            "uuid": record_uuid,
            "parentUuid": parent,
            "sessionId": str(conversation.id),
            "timestamp": stamp.astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "cwd": cwd,
            "version": version,
            "userType": "external",
            "isSidechain": False,
            "gitBranch": "",
            "message": payload,
        }
        records.append(record)
        parent = record_uuid
    return records


def session_lines(records: list[dict[str, Any]]) -> bytes:
    """Serialise records as JSONL, the way Claude Code writes them."""
    return "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records
    ).encode("utf-8")


def backup_name(now: datetime | None = None) -> str:
    """Timestamped directory name for a pre-import backup."""
    moment = now or datetime.now(UTC)
    return moment.strftime("%Y%m%dT%H%M%SZ")
