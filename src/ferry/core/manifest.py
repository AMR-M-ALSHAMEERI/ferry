"""Bundle manifest model, per PLAN.md §3.3."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ferry.ucs import ToolName

BUNDLE_VERSION: Literal["1.0"] = "1.0"

OSName = Literal["darwin", "linux", "win32"]


class SourceMachine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hostname: str | None = None
    os: OSName
    user_home: str | None = None


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bundle_version: Literal["1.0"] = BUNDLE_VERSION
    created_at: datetime
    created_by: str
    source_machine: SourceMachine
    conversation_count: int = 0
    tools_included: list[ToolName] = Field(default_factory=list)
    encrypted: bool = False
    encryption_algo: str | None = None
