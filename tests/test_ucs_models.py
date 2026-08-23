"""UCS model tests. Assertions are on real values, per PLAN.md §6.8.1."""

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from ferry.ucs import (
    UCS_VERSION,
    Attachment,
    Conversation,
    ImageBlock,
    Message,
    Provenance,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Workspace,
)


def test_version_is_1_3() -> None:
    assert UCS_VERSION == "1.3"


def test_conversation_defaults_to_current_version(conversation: Conversation) -> None:
    assert conversation.ucs_version == "1.3"


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


# --------------------------------------------------------------------------
# UCS 1.3: the image block
# --------------------------------------------------------------------------

IMAGE_ID = UUID("33333333-3333-4333-8333-333333333333")


def a_conversation_with_an_image() -> Conversation:
    return Conversation(
        id=UUID("11111111-1111-4111-8111-111111111111"),
        source_tool="claude-code",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        updated_at=datetime(2026, 8, 1, tzinfo=UTC),
        workspace=Workspace(),
        messages=[
            Message(
                role="user",
                content=[
                    ImageBlock(attachment_id=IMAGE_ID),
                    TextBlock(text="what is in this screenshot"),
                ],
            )
        ],
        attachments=[
            Attachment(
                id=IMAGE_ID,
                filename="image-0.png",
                mime_type="image/png",
                bundle_path="attachments/11111111-1111-4111-8111-111111111111/x.png",
                sha256="0" * 64,
            )
        ],
    )


def test_an_image_keeps_its_place_in_the_conversation() -> None:
    """The whole point of 1.3: an attachment list cannot say *where* a picture was."""
    restored = Conversation.model_validate_json(a_conversation_with_an_image().model_dump_json())

    blocks = restored.messages[0].content
    assert [b.type for b in blocks] == ["image", "text"]
    assert blocks[0].attachment_id == IMAGE_ID


def test_an_image_block_carries_no_bytes() -> None:
    """Re-embedding base64 would drag megabytes through every read and diff."""
    serialised = json.loads(a_conversation_with_an_image().model_dump_json())
    block = serialised["messages"][0]["content"][0]

    assert set(block) == {"type", "attachment_id"}


def test_the_image_block_points_at_a_real_attachment() -> None:
    conversation = a_conversation_with_an_image()
    referenced = {
        b.attachment_id for m in conversation.messages for b in m.content if b.type == "image"
    }

    assert referenced <= {a.id for a in conversation.attachments}


def test_a_tool_call_can_now_carry_its_own_id() -> None:
    """1.3 addition: before this, tool_result.tool_use_id named nothing in UCS."""
    call = ToolUseBlock(name="Grep", input={"pattern": "x"}, id="toolu_01")
    result = ToolResultBlock(tool_use_id="toolu_01", output="found")

    assert call.id == result.tool_use_id


def test_a_tool_call_without_an_id_still_validates() -> None:
    """The field is optional, so 1.2-shaped records are not rejected by it."""
    assert ToolUseBlock(name="Grep", input={}).id is None


def test_an_unknown_block_type_is_still_refused() -> None:
    """Adding a union member must not turn the discriminator permissive."""
    payload = json.loads(a_conversation_with_an_image().model_dump_json())
    payload["messages"][0]["content"][0] = {"type": "hologram", "data": "x"}

    with pytest.raises(ValidationError):
        Conversation.model_validate(payload)


def test_a_1_2_document_is_refused_rather_than_silently_upgraded() -> None:
    """1.2 bundles are not readable, and must fail loudly rather than by surprise."""
    payload = json.loads(a_conversation_with_an_image().model_dump_json())
    payload["ucs_version"] = "1.2"

    with pytest.raises(ValidationError):
        Conversation.model_validate(payload)
