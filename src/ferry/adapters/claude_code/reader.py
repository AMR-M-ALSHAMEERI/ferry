"""Turn one Claude Code session transcript into a UCS conversation.

The transcript is JSONL, one JSON object per line, and **not every line is a
message**. Ten record types were seen across 4,238 real records; only ``user``
and ``assistant`` carry conversation content. The rest are titles, mode
switches, queue operations, hook summaries and Claude Code's own context
injections. They are preserved verbatim in the bundle's ``source_raw`` so a
re-import is byte-perfect, but they are not messages and are not invented into
messages here.

Everything in this module is defensive by design. A single malformed line in a
transcript must cost the user that line, not the conversation -- so unknown
record types, unknown content blocks, unparseable JSON and missing fields all
produce a warning and a skip, never an exception.
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

from ferry.adapters.claude_code.paths import basename, sidecar_dir
from ferry.ucs import (
    Attachment,
    ContentBlock,
    Conversation,
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Workspace,
)

__all__ = ["MESSAGE_TYPES", "PendingAttachment", "SessionRead", "read_session"]

MESSAGE_TYPES = frozenset({"user", "assistant"})
"""Record types that carry conversation content. Everything else is metadata."""

_ATTACHMENT_NAMESPACE = UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
_EXTENSION_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
_UCS_ROLES = frozenset({"user", "assistant", "system", "tool"})


@dataclass(frozen=True)
class PendingAttachment:
    """An attachment extracted from the transcript, not yet written anywhere.

    Claude Code stores images inline as base64 inside the message, so there is
    no source file to copy -- the bytes come out of the JSON. The record and the
    bytes travel together until the bundle writes them.
    """

    record: Attachment
    data: bytes


@dataclass
class SessionRead:
    """Everything one transcript yielded."""

    conversation: Conversation | None
    attachments: list[PendingAttachment] = field(default_factory=list)
    sidecars: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _parse_timestamp(value: Any) -> datetime | None:
    """ISO 8601 with a trailing ``Z``. Never guesses -- unparseable is ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extension_for(mime: str) -> str:
    return _EXTENSION_BY_MIME.get(mime) or mimetypes.guess_extension(mime) or ".bin"


def _image_attachment(
    conversation_id: UUID, ordinal: int, source: dict[str, Any]
) -> PendingAttachment | None:
    """Decode one inline base64 image block, or ``None`` if it is not one.

    The attachment id is derived from the conversation id and the image's
    ordinal rather than generated fresh, so re-exporting the same session
    produces the same bundle instead of a pile of duplicate files.
    """
    if source.get("type") != "base64":
        return None
    data_text = source.get("data")
    mime = source.get("media_type")
    if not isinstance(data_text, str) or not isinstance(mime, str):
        return None
    try:
        data = base64.b64decode(data_text, validate=True)
    except (binascii.Error, ValueError):
        return None
    attachment_id = uuid5(_ATTACHMENT_NAMESPACE, f"{conversation_id}:image:{ordinal}")
    extension = _extension_for(mime)
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


def _block(raw: Any, out: SessionRead, conversation_id: UUID) -> ContentBlock | None:
    """Map one Claude Code content block onto UCS.

    Field names here were taken from a census of 2,327 real blocks, not from the
    public API docs -- ``thinking`` carries its text under ``thinking``, not
    ``text``, which is exactly the kind of detail that silently empties every
    reasoning block if assumed.
    """
    if not isinstance(raw, dict):
        out.warnings.append(f"content block was {type(raw).__name__}, not an object")
        return None
    kind = raw.get("type")
    if kind == "text":
        return TextBlock(text=str(raw.get("text", "")))
    if kind == "thinking":
        signature = raw.get("signature")
        return ThinkingBlock(
            text=str(raw.get("thinking", "")),
            signature=signature if isinstance(signature, str) else None,
        )
    if kind == "tool_use":
        name = raw.get("name")
        arguments = raw.get("input")
        call_id = raw.get("id")
        return ToolUseBlock(
            name=str(name) if name is not None else "",
            input=arguments if isinstance(arguments, dict) else {},
            # Carried since UCS 1.3, so the tool_result that follows names a
            # call this file actually identifies.
            id=call_id if isinstance(call_id, str) and call_id else None,
        )
    if kind == "tool_result":
        tool_use_id = raw.get("tool_use_id")
        return ToolResultBlock(
            tool_use_id=str(tool_use_id) if tool_use_id is not None else "",
            output=raw.get("content"),
        )
    if kind == "image":
        source = raw.get("source")
        if isinstance(source, dict):
            pending = _image_attachment(conversation_id, len(out.attachments), source)
            if pending is not None:
                out.attachments.append(pending)
                # The bytes go to the bundle; the block stays here, holding the
                # place in the conversation where the picture was. Before UCS
                # 1.3 there was nowhere to put this and the image showed up in
                # the attachment list with no indication of where it belonged.
                return ImageBlock(attachment_id=pending.record.id)
        out.warnings.append("image block could not be decoded; kept only in source_raw")
        return None
    out.warnings.append(f"unknown content block type {kind!r}; skipped")
    return None


def _content(raw: Any, out: SessionRead, conversation_id: UUID) -> list[ContentBlock]:
    """Normalise ``message.content``, which is a list -- except when it is not.

    74 of 2,399 real messages carried a bare string here. A reader that assumes
    a list drops those messages entirely and reports success.
    """
    if isinstance(raw, str):
        return [TextBlock(text=raw)]
    if not isinstance(raw, list):
        out.warnings.append(f"message.content was {type(raw).__name__}; skipped")
        return []
    blocks: list[ContentBlock] = []
    for item in raw:
        block = _block(item, out, conversation_id)
        if block is not None:
            blocks.append(block)
    return blocks


def _collect_sidecars(record: dict[str, Any], seen: set[Path], found: list[Path]) -> None:
    """Note any spilled tool output this record points at."""
    result = record.get("toolUseResult")
    if not isinstance(result, dict):
        return
    persisted = result.get("persistedOutputPath")
    if isinstance(persisted, str) and persisted:
        spilled = Path(persisted)
        if spilled not in seen:
            seen.add(spilled)
            found.append(spilled)


def read_session(path: Path) -> SessionRead:
    """Read one ``<session-uuid>.jsonl`` transcript.

    Args:
        path: The transcript. Its stem is the session UUID, which becomes the
            UCS conversation id -- stable, so re-exporting is idempotent and an
            interrupted export can resume by filename alone.

    Returns:
        A :class:`SessionRead`. ``conversation`` is ``None`` when the file held
        no usable messages; that is a skip, not an error.
    """
    out = SessionRead(conversation=None)

    try:
        conversation_id = UUID(path.stem)
    except ValueError:
        out.warnings.append(f"{path.name}: filename is not a session UUID; skipped")
        return out

    messages: list[Message] = []
    timestamps: list[datetime] = []
    cwd: str | None = None
    branch: str | None = None
    version: str | None = None
    custom_title: str | None = None
    ai_title: str | None = None
    referenced: list[Path] = []
    seen: set[Path] = set()

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

            stamp = _parse_timestamp(record.get("timestamp"))
            if stamp is not None:
                timestamps.append(stamp)
            if isinstance(record.get("cwd"), str) and record["cwd"]:
                cwd = record["cwd"]
            if isinstance(record.get("gitBranch"), str) and record["gitBranch"]:
                branch = record["gitBranch"]
            if isinstance(record.get("version"), str) and record["version"]:
                version = record["version"]
            if isinstance(record.get("customTitle"), str):
                custom_title = record["customTitle"]
            if isinstance(record.get("aiTitle"), str):
                ai_title = record["aiTitle"]
            _collect_sidecars(record, seen, referenced)

            if record.get("type") not in MESSAGE_TYPES:
                continue
            payload = record.get("message")
            if not isinstance(payload, dict):
                out.warnings.append(f"{path.name}:{number}: message record has no message")
                continue
            role = payload.get("role")
            if role not in _UCS_ROLES:
                out.warnings.append(f"{path.name}:{number}: unknown role {role!r}; skipped")
                continue
            model = payload.get("model")
            messages.append(
                Message(
                    role=role,
                    content=_content(payload.get("content"), out, conversation_id),
                    timestamp=stamp,
                    model=model if isinstance(model, str) else None,
                )
            )

    # Anything referenced but absent is reported rather than silently dropped: a
    # dangling sidecar reference is the failure this whole mechanism exists to
    # prevent, so it has to be visible before the bundle is written.
    for spilled in referenced:
        if spilled.is_file():
            out.sidecars.append(spilled)
        else:
            out.warnings.append(f"spilled tool output missing on disk: {spilled.name}")
    directory = sidecar_dir(path)
    if directory.is_dir():
        for extra in sorted(directory.glob("*")):
            if extra.is_file() and extra not in seen:
                out.sidecars.append(extra)

    if not messages:
        out.notes.append(f"{path.name}: no messages; skipped")
        return out
    if not timestamps:
        # created_at and updated_at are required and must not be invented.
        # A transcript with messages but no timestamp
        # anywhere has never been observed; if one appears, it is skipped loudly
        # rather than stamped with the clock.
        out.warnings.append(f"{path.name}: no timestamps anywhere; skipped")
        return out

    raw: dict[str, Any] = {"format": "claude-code-jsonl"}
    if branch:
        raw["git_branch"] = branch

    out.conversation = Conversation(
        id=conversation_id,
        source_tool="claude-code",
        source_tool_version=version,
        source_id=path.stem,
        title=custom_title or ai_title,
        created_at=min(timestamps),
        updated_at=max(timestamps),
        workspace=Workspace(
            name=basename(cwd) if cwd else None,
            original_path=cwd,
            path_hash=path.parent.name,
        ),
        messages=messages,
        attachments=[pending.record for pending in out.attachments],
        source_raw=raw,
    )
    return out
