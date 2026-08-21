"""UCS model tests. Assertions are on real values, per PLAN.md §6.8.1."""

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from ferry.ucs import (
    UCS_VERSION,
    Conversation,
    Message,
    Provenance,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Workspace,
)


def test_version_is_1_2() -> None:
    assert UCS_VERSION == "1.2"


def test_conversation_defaults_to_current_version(conversation: Conversation) -> None:
    assert conversation.ucs_version == "1.2"


def test_all_four_block_types_survive_serialisation(conversation: Conversation) -> None:
    """Round-trip through JSON must preserve block types, order, and payloads."""
    restored = Conversation.model_validate_json(conversation.model_dump_json())

    assert [m.role for m in restored.messages] == ["user", "assistant", "tool"]

    assistant_blocks = restored.messages[1].content
    assert [b.type for b in assistant_blocks] == ["thinking", "text", "tool_use"]

    thinking = assistant_blocks[0]
    assert isinstance(thinking, ThinkingBlock)
    assert thinking.text == "reasoning text"
    assert thinking.signature == "sig-abc"

    text = assistant_blocks[1]
    assert isinstance(text, TextBlock)
    assert text.text == "assistant reply"

    tool_use = assistant_blocks[2]
    assert isinstance(tool_use, ToolUseBlock)
    assert tool_use.name == "Bash"
    assert tool_use.input == {"command": "ls"}

    tool_result = restored.messages[2].content[0]
    assert isinstance(tool_result, ToolResultBlock)
    assert tool_result.tool_use_id == "tool-1"
    assert tool_result.output == "file-a\nfile-b"


def test_thinking_signature_may_be_absent() -> None:
    """Cross-tool import drops signatures (M7b) — the model must accept that."""
    block = ThinkingBlock(text="reasoning with no signature")
    assert block.signature is None


def test_null_timestamp_is_preserved_not_invented(conversation: Conversation) -> None:
    """PLAN.md §3.2: never fabricate a timestamp the source did not store."""
    restored = Conversation.model_validate_json(conversation.model_dump_json())
    assert restored.messages[2].timestamp is None
    assert json.loads(conversation.model_dump_json())["messages"][2]["timestamp"] is None


def test_provenance_absent_by_default(conversation: Conversation) -> None:
    assert conversation.provenance is None
    assert json.loads(conversation.model_dump_json())["provenance"] is None


def test_provenance_round_trips_with_notes(conversation: Conversation) -> None:
    conversation.provenance = Provenance(
        original_tool="claude-code",
        imported_into="codex",
        imported_at=datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC),
        ferry_version="0.1.0",
        lossy=True,
        conversion_notes=["thinking signatures dropped"],
    )
    restored = Conversation.model_validate_json(conversation.model_dump_json())
    assert restored.provenance is not None
    assert restored.provenance.original_tool == "claude-code"
    assert restored.provenance.imported_into == "codex"
    assert restored.provenance.lossy is True
    assert restored.provenance.conversion_notes == ["thinking signatures dropped"]


def test_unknown_source_tool_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Conversation(
            id=UUID("33333333-3333-4333-8333-333333333333"),
            source_tool="cursor",  # type: ignore[arg-type]
            created_at=datetime(2026, 8, 20, tzinfo=UTC),
            updated_at=datetime(2026, 8, 20, tzinfo=UTC),
            workspace=Workspace(),
        )


def test_unknown_block_type_is_rejected() -> None:
    """An adapter emitting a block type UCS does not define must fail loudly, not silently."""
    with pytest.raises(ValidationError):
        Message.model_validate({"role": "user", "content": [{"type": "video", "url": "x"}]})


def test_extra_fields_are_rejected() -> None:
    """extra='forbid' catches adapter typos instead of silently dropping data."""
    with pytest.raises(ValidationError):
        Workspace.model_validate({"name": "p", "orignal_path": "typo"})
