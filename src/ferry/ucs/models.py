"""Pydantic models for the Universal Conversation Schema (UCS), per PLAN.md §3.1.

UCS is the interchange format every adapter exports to and imports from.
Schema version tracked here must match the version documented in PLAN.md §3
and the generated JSON schema at schemas/ucs-<version>.json.
"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

UCS_VERSION: Literal["1.2"] = "1.2"

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
    model_config = ConfigDict(extra="forbid")
    type: Literal["tool_use"] = "tool_use"
    name: str
    input: dict[str, Any]


class ToolResultBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    output: Any


ContentBlock = Annotated[
    TextBlock | ThinkingBlock | ToolUseBlock | ToolResultBlock,
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
    See PLAN.md §3.2 "never present a foreign conversation as native".
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
    ucs_version: Literal["1.2"] = UCS_VERSION
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
