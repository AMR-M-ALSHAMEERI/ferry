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

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from ferry.adapters.claude_code.paths import basename
from ferry.core.continuable import call_line
from ferry.ucs import Attachment, Conversation, Message

__all__ = [
    "SYNTHESIS_NOTES",
    "missing_images",
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
            name = basename(str(result["persistedOutputPath"]))
            moved["persistedOutputPath"] = str(Path(remap.new_sidecar_root) / name)
            out["toolUseResult"] = moved
            remap.rewrites += 1
    return out


def _tool_use_id(conversation_id: UUID, ordinal: int) -> str:
    """A stable stand-in id for a tool call UCS did not preserve."""
    digest = hashlib.sha256(f"{conversation_id}:{ordinal}".encode()).hexdigest()
    return f"toolu_{digest[:24]}"


def _blocks_for(
    message: Message,
    conversation_id: UUID,
    counter: list[int],
    images: Mapping[UUID, dict[str, Any]],
    *,
    foreign: str = "",
) -> list[Any]:
    """The message's blocks as Claude Code records them.

    ``foreign`` names the tool the conversation came from, and is empty for a
    restore. **It changes what a tool call becomes.** A restore writes the call
    back as a call, which is what it was. A conversion cannot: the call was made
    by another assistant, to a tool Claude Code does not have, and writing it as
    a native ``tool_use`` puts a call Claude Code never made into its transcript
    in the exact shape of one it did. It also contradicts the sentence shown
    before the person agreed -- *kept as readable text, not as tool calls Claude
    Code can run* -- and of those two documents, the file is the one that lasts.

    The line is built by the same function Continue mode uses, so the two modes
    describe a call identically rather than drifting apart.
    """
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
            if foreign:
                blocks.append({"type": "text", "text": call_line(foreign, block.name, block.input)})
                continue
            blocks.append(
                {
                    "type": "tool_use",
                    # The original id when UCS 1.3 carried one; a generated
                    # stand-in only when it did not. The stand-in keeps a call
                    # and its result linked, but it is not what the tool wrote.
                    "id": block.id or _tool_use_id(conversation_id, counter[0]),
                    "name": block.name,
                    "input": block.input,
                }
            )
        elif block.type == "tool_result":
            if foreign:
                # Kept whole rather than clipped: archive mode's whole promise
                # is that it holds the most detail, and a result is where the
                # detail is. What changes is its shape, not its content.
                text = block.output if isinstance(block.output, str) else json.dumps(block.output)
                blocks.append({"type": "text", "text": text})
                continue
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": block.output,
                }
            )
        elif block.type == "image":
            image = images.get(block.attachment_id)
            if image is not None:
                blocks.append(image)
            # An image whose bytes are not in the bundle is dropped rather than
            # written as a broken reference. The caller reports it; see
            # missing_images() below.
    return blocks


def _encoded_image(attachment: Attachment, data: bytes) -> dict[str, Any]:
    """Rebuild the inline base64 block Claude Code stores images as."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": attachment.mime_type,
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def missing_images(conversation: Conversation, image_bytes: Mapping[UUID, bytes]) -> list[UUID]:
    """Image blocks whose bytes are not available to write back."""
    return [
        block.attachment_id
        for message in conversation.messages
        for block in message.content
        if block.type == "image" and block.attachment_id not in image_bytes
    ]


def synthesize_records(
    conversation: Conversation,
    *,
    cwd: str,
    version: str,
    image_bytes: Mapping[UUID, bytes] | None = None,
    target: str = "",
) -> list[dict[str, Any]]:
    """Build transcript records from UCS alone.

    The threading is rebuilt as a straight chain -- ``parentUuid`` pointing at
    the previous record -- because UCS stores messages as an ordered list and
    does not carry the original tree. A branched conversation therefore comes
    back linear. That is a real loss and it is named in :data:`SYNTHESIS_NOTES`
    rather than hidden.

    Args:
        image_bytes: Attachment bytes read from the bundle, keyed by attachment
            id. Claude Code stores images inline, so they have to be re-encoded
            into the record rather than referenced. An image with no bytes is
            omitted rather than written as a dangling reference -- ask
            :func:`missing_images` first if you want to report that.
    """
    available = image_bytes or {}
    images = {
        attachment.id: _encoded_image(attachment, available[attachment.id])
        for attachment in conversation.attachments
        if attachment.id in available
    }
    records: list[dict[str, Any]] = []
    parent: str | None = None
    counter = [0]
    for index, message in enumerate(conversation.messages):
        record_uuid = str(uuid5(_RECORD_NAMESPACE, f"{conversation.id}:{index}"))
        stamp = message.timestamp or conversation.created_at
        role = message.role
        payload: dict[str, Any] = {
            "role": "assistant" if role == "assistant" else "user",
            "content": _blocks_for(
                message,
                conversation.id,
                counter,
                images,
                foreign=""
                if not target or conversation.source_tool == target
                else conversation.source_tool,
            ),
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
