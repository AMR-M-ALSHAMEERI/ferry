"""Shared fixtures. Synthetic UCS objects only — no real conversation data (PLAN.md §6.6)."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from ferry.core import Manifest, SourceMachine
from ferry.ucs import (
    Attachment,
    Conversation,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Workspace,
)

CONV_ID = UUID("11111111-1111-4111-8111-111111111111")
ATTACH_ID = UUID("22222222-2222-4222-8222-222222222222")


@pytest.fixture
def manifest() -> Manifest:
    return Manifest(
        created_at=datetime(2026, 8, 21, 10, 0, 0, tzinfo=UTC),
        created_by="ferry v0.1.0",
        source_machine=SourceMachine(hostname="testhost", os="win32", user_home="C:/Users/test"),
    )


@pytest.fixture
def conversation() -> Conversation:
    """A conversation exercising all four content block types."""
    return Conversation(
        id=CONV_ID,
        source_tool="claude-code",
        source_tool_version="2.1.234",
        source_id="session-abc",
        title="Synthetic test conversation",
        created_at=datetime(2026, 8, 20, 9, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 20, 9, 30, 0, tzinfo=UTC),
        workspace=Workspace(
            name="proj",
            original_path="C:/Users/test/proj",
            path_hash="C--Users-test-proj",
        ),
        messages=[
            Message(
                role="user",
                content=[TextBlock(text="first user turn")],
                timestamp=datetime(2026, 8, 20, 9, 0, 0, tzinfo=UTC),
            ),
            Message(
                role="assistant",
                content=[
                    ThinkingBlock(text="reasoning text", signature="sig-abc"),
                    TextBlock(text="assistant reply"),
                    ToolUseBlock(name="Bash", input={"command": "ls"}),
                ],
                timestamp=datetime(2026, 8, 20, 9, 1, 0, tzinfo=UTC),
                model="claude-opus-5",
            ),
            Message(
                role="tool",
                content=[ToolResultBlock(tool_use_id="tool-1", output="file-a\nfile-b")],
                timestamp=None,
            ),
        ],
    )


@pytest.fixture
def attachment_record() -> Attachment:
    """sha256 is filled in by the test once the file exists — this is the shape only."""
    return Attachment(
        id=ATTACH_ID,
        filename="shot.png",
        mime_type="image/png",
        bundle_path=f"attachments/{CONV_ID}/{ATTACH_ID}.png",
        sha256="",
    )
