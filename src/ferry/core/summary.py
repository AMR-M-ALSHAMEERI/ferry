"""What is inside a bundle, read without importing it.

The import screen can say how many conversations a bundle holds and which tools
they came from, because the manifest records both. It cannot say what they
*are* -- their titles, when they happened, which folders they were recorded in,
how much of the bundle each one accounts for. Until now nothing could, so the
only way to find out what you had backed up was to import it somewhere and
look.

**Read with `json.loads`, not through the UCS models.** Two reasons, and both
matter here. A 53 MB Codex conversation validated through pydantic builds tens
of thousands of model objects to answer six questions. And a conversation that
*fails* validation still has to appear in this list -- a bundle you cannot read
is precisely the one you need to look at, and possibly the one you want to
delete.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from ferry.core.bundle import Bundle
from ferry.core.manifest import Manifest

__all__ = ["BundleSummary", "ConversationSummary", "summarise"]


@dataclass(frozen=True)
class ConversationSummary:
    """One conversation, as much as can be said without loading it fully."""

    id: UUID
    title: str | None = None
    tool: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    messages: int = 0
    workspace: str | None = None
    attachments: int = 0
    has_source_raw: bool = False
    bytes_on_disk: int = 0
    """The conversation document, its attachments and its original file
    together -- what deleting it would actually free."""

    unreadable: str | None = None
    """Why this one could not be read, if it could not. It is still listed:
    a conversation Ferry cannot parse is the one most worth seeing."""

    @property
    def name(self) -> str:
        """Something to show in a list. Never blank, never a bare uuid alone."""
        if self.title:
            return self.title
        return f"untitled ({str(self.id)[:8]})"


@dataclass(frozen=True)
class BundleSummary:
    """A whole bundle, ready to render."""

    root: Path
    manifest: Manifest
    conversations: list[ConversationSummary] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    """Whatever :meth:`Bundle.validate` found. Shown, never hidden -- a bundle
    with a fault is still worth looking at, and the fault is the reason."""

    bytes_on_disk: int = 0

    @property
    def folders(self) -> list[str]:
        """The distinct working directories these conversations were recorded in.

        Deferred here from the import screen deliberately. Answering it means
        opening every conversation file, which is affordable when the whole
        point of the screen is to look inside and not when someone is trying to
        get through a restore.
        """
        seen: dict[str, None] = {}
        for conversation in self.conversations:
            if conversation.workspace:
                seen.setdefault(conversation.workspace, None)
        return list(seen)

    @property
    def by_tool(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for conversation in self.conversations:
            counts[conversation.tool or "unknown"] = (
                counts.get(conversation.tool or "unknown", 0) + 1
            )
        return counts


def _directory_bytes(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _file_bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _summarise_one(bundle: Bundle, conversation_id: UUID) -> ConversationSummary:
    document = bundle.conversation_path(conversation_id)
    footprint = (
        _file_bytes(document)
        + _directory_bytes(bundle.root / "attachments" / str(conversation_id))
        + _file_bytes(bundle.source_raw_path(conversation_id))
        + _directory_bytes(bundle.source_raw_sidecar_dir(conversation_id))
    )

    try:
        raw = json.loads(document.read_bytes())
    except (OSError, ValueError) as exc:
        return ConversationSummary(id=conversation_id, bytes_on_disk=footprint, unreadable=str(exc))
    if not isinstance(raw, dict):
        return ConversationSummary(
            id=conversation_id,
            bytes_on_disk=footprint,
            unreadable="the conversation file is not a JSON object",
        )

    workspace = raw.get("workspace")
    messages = raw.get("messages")
    attachments = raw.get("attachments")
    title = raw.get("title")
    return ConversationSummary(
        id=conversation_id,
        title=title if isinstance(title, str) and title.strip() else None,
        tool=raw.get("source_tool") if isinstance(raw.get("source_tool"), str) else None,
        created_at=_moment(raw.get("created_at")),
        updated_at=_moment(raw.get("updated_at")),
        messages=len(messages) if isinstance(messages, list) else 0,
        workspace=(
            workspace.get("original_path")
            if isinstance(workspace, dict) and isinstance(workspace.get("original_path"), str)
            else None
        ),
        attachments=len(attachments) if isinstance(attachments, list) else 0,
        has_source_raw=bundle.has_source_raw(conversation_id),
        bytes_on_disk=footprint,
    )


def summarise(bundle: Bundle, *, validate: bool = True) -> BundleSummary:
    """Read a bundle end to end and describe it.

    Args:
        bundle: An open bundle.
        validate: Whether to run :meth:`Bundle.validate` as well. On by
            default -- someone looking inside a bundle wants to know if it is
            broken, and this is the only screen that would tell them.
    """
    conversations = [_summarise_one(bundle, cid) for cid in bundle.list_conversations()]
    return BundleSummary(
        root=bundle.root,
        manifest=bundle.manifest,
        conversations=conversations,
        problems=bundle.validate() if validate else [],
        bytes_on_disk=_directory_bytes(bundle.root),
    )
