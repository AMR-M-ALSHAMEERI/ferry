"""Pydantic models for the Universal Conversation Schema (UCS).

UCS is the interchange format every adapter exports to and imports from.
Schema version tracked here must match the generated JSON schema at
schemas/ucs-<version>.json.
"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

UCS_VERSION: Literal["1.3"] = "1.3"
"""Bumped to 1.3 for the ``image`` content block.

Adding a *field* is additive and needs no bump (§3.2 of the plan). Adding a
member to a discriminated union is not: this model rejects unknown ``type``
values outright, so a reader built for 1.2 would refuse a conversation
containing an image rather than ignore it. Version numbers exist to say that.

**1.2 bundles cannot be read by 1.3.** No migration path is provided because
none is owed — 1.2 has never been released, and writing a converter for a
schema no bundle in the world uses is speculative work.
"""

ToolName = Literal["claude-code", "codex", "copilot", "antigravity"]
Role = Literal["user", "assistant", "system", "tool"]


class TextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text"] = "text"
    text: str


class ThinkingBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["thinking"] = "thinking"
    text: str
    signature: str | None = None


class ToolUseBlock(BaseModel):
    """A tool call.

    ``id`` was added in 1.3 and is optional, so 1.2 records still validate. It
    exists because :class:`ToolResultBlock` carries a ``tool_use_id`` that,
    until now, named a call no UCS block identified — the link between a call
    and its result was expressible only in the original format. Adapters that
    have the source id should preserve it.
    """

    model_config = ConfigDict(extra="forbid")
    type: Literal["tool_use"] = "tool_use"
    name: str
    input: dict[str, Any]
    id: str | None = None


class ToolResultBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    output: Any


class ImageBlock(BaseModel):
    """An image, in the position it occupied in the conversation.

    The bytes are not here. They live in the bundle as a real file, listed in
    :attr:`Conversation.attachments` and checksummed like any other attachment;
    this block records only *where in the conversation the picture was*, which
    is the part an attachment list cannot express.

    Keeping the two apart matters for size as much as for tidiness: tools store
    images inline as base64, and a UCS file that re-embedded them would carry
    megabytes of encoded pixels through every read, diff and round-trip.

    ``attachment_id`` must match an ``Attachment.id`` on the same conversation.
    """

    model_config = ConfigDict(extra="forbid")
    type: Literal["image"] = "image"
    attachment_id: UUID


ContentBlock = Annotated[
    TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock | ImageBlock,
    Field(discriminator="type"),
]


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role
    content: list[ContentBlock]
    timestamp: datetime | None = None
    model: str | None = None


class Workspace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    original_path: str | None = None
    path_hash: str | None = None


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    filename: str
    mime_type: str
    bundle_path: str
    sha256: str


class Provenance(BaseModel):
    """Populated by the IMPORTING adapter when target tool != source_tool.

    Absent means the conversation has only ever lived in its source_tool.
    The rule it exists for: never present a foreign conversation as native.
    """

    model_config = ConfigDict(extra="forbid")
    original_tool: ToolName
    imported_into: ToolName
    imported_at: datetime
    ferry_version: str
    lossy: bool
    conversion_notes: list[str] = Field(default_factory=list)


class Conversation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ucs_version: Literal["1.3"] = UCS_VERSION
    id: UUID
    source_tool: ToolName
    source_tool_version: str | None = None
    source_id: str = ""
    title: str | None = None
    created_at: datetime
    updated_at: datetime
    workspace: Workspace
    messages: list[Message] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    provenance: Provenance | None = None
    source_raw: dict[str, Any] | None = None
