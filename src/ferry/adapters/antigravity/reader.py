"""Turning one Antigravity conversation database into UCS.

The division of labour here is worth stating plainly, because it is different
from the other three adapters.

**UCS is the readable view. The database itself is the lossless one.** Ferry
has no schema for these blobs and can never prove it understood all of them, so
the export copies the original database into the bundle as ``source_raw`` and
re-imports *that*, rewriting paths inside it. The messages built here are what
a person reads in ``ferry inspect`` and what a cross-tool import consumes; they
are not the thing an Antigravity-to-Antigravity migration is reconstructed
from.

That is why this module is allowed to be selective -- skipping a 758 KB
CHECKPOINT blob that holds file snapshots rather than conversation -- without
that selectivity costing anything on re-import.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote
from uuid import UUID, uuid5

from ferry.adapters.antigravity import paths, schema
from ferry.adapters.antigravity.wire import parse, strings
from ferry.adapters.pathutil import basename
from ferry.ucs import (
    Attachment,
    Conversation,
    ImageBlock,
    Message,
    TextBlock,
    ToolUseBlock,
    Workspace,
)

__all__ = [
    "PendingAttachment",
    "SessionRead",
    "parent_conversation",
    "read_conversation",
    "step_text",
]

ContentBlockValue = TextBlock | ToolUseBlock
"""What a step becomes: prose, or a record that the agent did something."""

_TITLE_LIMIT = 80

_ATTACHMENT_NAMESPACE = UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
"""Namespace for deriving an attachment id from its conversation and filename.

Deterministic on purpose. A random id means two exports of the same unchanged
history produce different bundles, which makes them impossible to compare and
was a real defect in the Copilot adapter before it was caught.
"""

_META_CREATED = (2, 1)
"""Where ``trajectory_metadata_blob`` keeps the conversation's start, epoch seconds."""

_META_PROJECT = (18,)
"""Where it names the project -- true for 6 of 6 conversations on the probe machine."""


def parent_conversation(
    database: Path, env: os._Environ[str] | dict[str, str] | None = None
) -> str | None:
    """The conversation that spawned this one, or ``None`` if it is top-level.

    Reads one row rather than the whole conversation, because the count shown
    on the detection screen depends on this and running it over every database
    must stay cheap.

    Two fields say the same thing from opposite directions -- field 5 names the
    parent and is absent at the top, field 6 names the root and is the
    conversation's own id at the top. **Both must agree**, and a database where
    they disagree is treated as top-level: showing a conversation that turns
    out to be a subagent is a much smaller harm than hiding one that is real.
    """
    try:
        with paths.open_readonly(database) as connection:
            row = connection.execute("SELECT data FROM trajectory_metadata_blob LIMIT 1").fetchone()
    except (sqlite3.Error, OSError):
        return None
    if not row or not isinstance(row[0], bytes | bytearray):
        return None

    blob = bytes(row[0])
    parent = _string_at(blob, schema.PARENT_CONVERSATION)
    root = _string_at(blob, schema.ROOT_CONVERSATION)
    own = database.stem

    if parent is None or parent == own:
        return None
    if root is not None and root != parent:
        return None
    return parent


@dataclass
class PendingAttachment:
    """An uploaded image, still sitting in the conversation's brain directory.

    Unlike the other three tools, Antigravity stores attachments as real files
    rather than base64 inside the transcript, so the bytes are not carried here
    -- only where to find them.
    """

    record: Attachment
    source: Path


@dataclass
class SessionRead:
    """Everything one conversation database yielded."""

    conversation: Conversation | None
    attachments: list[PendingAttachment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    project_id: str | None = None
    """Which project the conversation belongs to, for the importer to recreate."""

    parent_id: str | None = None
    """The conversation that spawned this one, if it is a subagent trajectory."""

    checkpoints: int = 0
    """Steps holding file snapshots rather than conversation, left out of the messages."""

    unknown_types: tuple[int, ...] = ()
    """Step types this adapter has no name for, read as assistant messages."""


def _varint(value: bytes) -> int:
    number = 0
    for index, byte in enumerate(value):
        number |= (byte & 0x7F) << (7 * index)
    return number


def _varint_at(blob: bytes, path: tuple[int, ...]) -> int | None:
    """The varint at a field path, or ``None`` if it is not there."""
    fields = parse(blob)
    if fields is None:
        return None
    head, rest = path[0], path[1:]
    for item in fields:
        if item.number != head:
            continue
        if not rest:
            if item.wire == 0:
                return _varint(item.value)
            continue
        if item.wire == 2:
            found = _varint_at(item.value, rest)
            if found is not None:
                return found
    return None


def _string_at(blob: bytes, path: tuple[int, ...]) -> str | None:
    """The first non-empty string at a field path."""
    for found, text in strings(blob):
        if found == path and text.strip():
            return text
    return None


def _moment(seconds: int | None) -> datetime | None:
    """An epoch-second field as a UTC time, or ``None`` if it is not plausible.

    Bounded rather than trusted. A varint read at the wrong path decodes into a
    perfectly valid integer, and an unbounded conversion turns that into a
    conversation dated in the year 12,000 -- which sorts to the top of every
    listing the user sees.
    """
    if seconds is None or not 946_684_800 < seconds < 4_102_444_800:
        return None
    return datetime.fromtimestamp(seconds, UTC)


def _folder_path(uri: str | None) -> str | None:
    """A ``folderUri`` as the absolute path it names.

    Antigravity writes these percent-encoded with backslashes
    (``file:///c%3A%5CUsers%5C...``), which is a spelling nothing else in Ferry
    produces and which reads as gibberish if handed straight to the user.
    """
    if not uri:
        return None
    text = unquote(uri)
    for prefix in ("file:///", "file://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    if len(text) > 2 and text[1] == ":":
        text = text[0].upper() + text[1:]
    return text or None


def step_text(payload: bytes, step_type: int) -> str | None:
    """A step's text, from the first field path that has any.

    Several step types record the same text twice, and which copy is populated
    varies, so the paths are tried in the order :data:`schema.TEXT_FIELDS`
    lists them rather than being treated as alternatives to merge.
    """
    for path in schema.TEXT_FIELDS.get(step_type, ()):
        text = _string_at(payload, path)
        if text:
            return text
    return None


def _title(messages: list[Message]) -> str | None:
    """The conversation's name: the first thing the user typed, trimmed.

    Antigravity shows a generated title in its own sidebar and does not store
    it anywhere in the database, so there is nothing better to use. A truncated
    first line still tells the user which conversation this is; no title at all
    does not.
    """
    for message in messages:
        if message.role != "user":
            continue
        for block in message.content:
            if isinstance(block, TextBlock) and block.text.strip():
                text = " ".join(block.text.split())
                return text if len(text) <= _TITLE_LIMIT else text[: _TITLE_LIMIT - 3] + "..."
    return None


def _attachments(
    conversation_id: UUID,
    source_id: str,
    env: os._Environ[str] | dict[str, str] | None,
) -> list[PendingAttachment]:
    """Images the user uploaded, as records pointing at the files on disk."""
    found: list[PendingAttachment] = []
    for path in paths.attachment_files(source_id, env):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        attachment_id = uuid5(_ATTACHMENT_NAMESPACE, f"{conversation_id}:{path.name}")
        suffix = path.suffix.lstrip(".") or "bin"
        found.append(
            PendingAttachment(
                record=Attachment(
                    id=attachment_id,
                    filename=path.name,
                    mime_type=mime,
                    bundle_path=f"attachments/{conversation_id}/{attachment_id}.{suffix}",
                    sha256=hashlib.sha256(data).hexdigest(),
                ),
                source=path,
            )
        )
    return found


def read_conversation(
    database: Path, env: os._Environ[str] | dict[str, str] | None = None
) -> SessionRead:
    """Read one conversation database into UCS.

    Never raises. A database that cannot be opened comes back as a
    ``conversation`` of ``None`` with the reason in ``warnings``, because one
    unreadable conversation must not stop the export of the others.
    """
    conversation_id = paths.conversation_id_of(database)
    if conversation_id is None:
        return SessionRead(None, warnings=[f"{database.name} is not named after a conversation id"])

    source_id = database.stem
    warnings: list[str] = []
    notes: list[str] = []
    messages: list[Message] = []
    skipped_checkpoints = 0
    unknown_types: set[int] = set()

    try:
        with paths.open_readonly(database) as connection:
            meta = connection.execute(
                "SELECT data FROM trajectory_metadata_blob LIMIT 1"
            ).fetchone()
            rows = connection.execute(
                "SELECT idx, step_type, metadata, step_payload FROM steps ORDER BY idx"
            ).fetchall()
    except (sqlite3.Error, OSError) as error:
        return SessionRead(None, warnings=[f"{database.name} could not be read: {error}"])

    meta_blob = bytes(meta[0]) if meta and isinstance(meta[0], bytes | bytearray) else b""
    created = _moment(_varint_at(meta_blob, _META_CREATED)) if meta_blob else None
    project_id = _string_at(meta_blob, _META_PROJECT) if meta_blob else None

    latest = created
    for index, step_type, metadata, payload in rows:
        if step_type == schema.CHECKPOINT:
            skipped_checkpoints += 1
            continue
        if step_type not in schema.STEP_TYPES:
            unknown_types.add(step_type)

        moment = None
        if isinstance(metadata, bytes | bytearray):
            moment = _moment(_varint_at(bytes(metadata), schema.STEP_TIMESTAMP))
        if moment is not None and (latest is None or moment > latest):
            latest = moment

        if not isinstance(payload, bytes | bytearray) or not payload:
            continue
        text = step_text(bytes(payload), step_type)
        if not text or not text.strip():
            continue

        role = schema.role_of(step_type)
        block: ContentBlockValue
        if role == "tool":
            block = ToolUseBlock(
                name=schema.type_name(step_type),
                input={"detail": text},
                id=str(index),
            )
        else:
            block = TextBlock(text=text)
        messages.append(Message(role=role, content=[block], timestamp=moment))

    attachments = _attachments(conversation_id, source_id, env)
    if attachments:
        # Antigravity records which step an upload belonged to nowhere Ferry
        # could find, so the images are listed on the conversation and placed
        # at the end rather than being put in a position that would be a guess.
        messages.append(
            Message(
                role="user",
                content=[ImageBlock(attachment_id=pending.record.id) for pending in attachments],
                timestamp=latest,
            )
        )

    # Counted here, phrased once by the adapter. The same sentence repeated
    # per conversation with a different number in front of it is not several
    # pieces of information, and it was drowning the export screen.

    parent = parent_conversation(database, env)

    known = paths.projects(env).get(project_id or "")
    folder = _folder_path(known.folder) if known else None
    workspace = Workspace(
        name=basename(folder) if folder else (known.name if known else None),
        original_path=folder,
    )

    conversation = Conversation(
        id=conversation_id,
        source_tool="antigravity",
        source_tool_version=paths.antigravity_version(env),
        source_id=source_id,
        title=_title(messages),
        created_at=created or datetime.fromtimestamp(database.stat().st_mtime, UTC),
        updated_at=latest or created or datetime.fromtimestamp(database.stat().st_mtime, UTC),
        workspace=workspace,
        messages=messages,
        attachments=[pending.record for pending in attachments],
        source_raw={
            "database": database.name,
            "project_id": project_id,
            "parent_conversation": parent,
            "steps": len(rows),
        },
    )

    return SessionRead(
        conversation=conversation,
        attachments=attachments,
        warnings=warnings,
        notes=notes,
        project_id=project_id,
        parent_id=parent,
        checkpoints=skipped_checkpoints,
        unknown_types=tuple(sorted(unknown_types)),
    )
