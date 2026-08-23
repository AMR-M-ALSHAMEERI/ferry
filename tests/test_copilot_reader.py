"""Reading a replayed Copilot session into UCS.

The delta replay has its own tests; these cover the mapping, where the two
non-obvious rules live:

- The assistant's prose is an untagged ``MarkdownString``. Matching on ``kind``
  keeps the tool calls and drops the answer.
- Not every block is conversation. ``mcpServersStarting`` is the interface
  narrating itself, and turning it into a message invents one nobody wrote.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from ferry.adapters.copilot.reader import read_session
from ferry.ucs import ImageBlock, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock

FIXTURES = Path(__file__).parent / "fixtures" / "copilot"
SESSION_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")


@pytest.fixture
def session(tmp_path: Path):
    """Copy a fixture to a UUID filename, since the name is the id."""

    def place(fixture: str, name: UUID = SESSION_ID) -> Path:
        target = tmp_path / f"{name}.jsonl"
        target.write_bytes((FIXTURES / fixture).read_bytes())
        return target

    return place


def texts(message) -> list[str]:  # type: ignore[no-untyped-def]
    return [b.text for b in message.content if isinstance(b, TextBlock)]


# --------------------------------------------------------------------------
# the ordinary case
# --------------------------------------------------------------------------


def test_a_conversation_is_read_with_both_sides(session) -> None:  # type: ignore[no-untyped-def]
    found = read_session(session("basic.jsonl"))

    assert found.conversation is not None
    roles = [m.role for m in found.conversation.messages]
    assert roles == ["user", "assistant", "user", "assistant"]


def test_untagged_markdown_is_the_assistant_speaking(session) -> None:  # type: ignore[no-untyped-def]
    """The block carrying the answer has no `kind`. A reader that switches on
    `kind` keeps the tool calls and silently drops the reply."""
    found = read_session(session("basic.jsonl"))
    assert found.conversation is not None

    assert "Use slicing:" in texts(found.conversation.messages[1])


def test_a_markdown_block_with_extra_flags_is_still_text(session) -> None:  # type: ignore[no-untyped-def]
    """Three different key sets appear for this type in real data. Matching an
    exact set would drop the answer the moment VS Code adds a flag."""
    found = read_session(session("basic.jsonl"))
    assert found.conversation is not None

    assert "The file defines one function." in texts(found.conversation.messages[3])


def test_a_tool_call_becomes_a_call_and_its_result(session) -> None:  # type: ignore[no-untyped-def]
    found = read_session(session("basic.jsonl"))
    assert found.conversation is not None
    blocks = found.conversation.messages[3].content

    calls = [b for b in blocks if isinstance(b, ToolUseBlock)]
    results = [b for b in blocks if isinstance(b, ToolResultBlock)]
    assert [c.name for c in calls] == ["copilot_readFile"]
    assert calls[0].id == "call-1"
    assert [r.tool_use_id for r in results] == ["call-1"]


def test_interface_narration_is_dropped_but_counted(session) -> None:  # type: ignore[no-untyped-def]
    """`mcpServersStarting` is VS Code talking about itself. It must not become
    a message, and it must not vanish without a word either."""
    found = read_session(session("basic.jsonl"))

    assert found.dropped_kinds.get("mcpServersStarting") == 1
    assert any("mcpServersStarting" in note for note in found.notes)


def test_a_custom_title_is_used(session) -> None:  # type: ignore[no-untyped-def]
    found = read_session(session("basic.jsonl"))
    assert found.conversation is not None

    assert found.conversation.title == "Reversing a list"


def test_timestamps_convert_from_epoch_milliseconds(session) -> None:  # type: ignore[no-untyped-def]
    found = read_session(session("basic.jsonl"))
    assert found.conversation is not None

    assert found.conversation.created_at.year == 2026
    assert found.conversation.messages[0].timestamp is not None


def test_the_whole_replayed_document_is_carried_for_re_import(session) -> None:  # type: ignore[no-untyped-def]
    """These transcripts are small -- 1.65 MB for ten real ones -- so the
    document itself travels, and an import does not have to reconstruct it."""
    found = read_session(session("basic.jsonl"))
    assert found.conversation is not None
    raw = found.conversation.source_raw

    assert raw is not None
    assert len(raw["document"]["requests"]) == 2


# --------------------------------------------------------------------------
# the splice
# --------------------------------------------------------------------------


def test_a_revised_response_is_not_duplicated(session) -> None:  # type: ignore[no-untyped-def]
    """Reading `i` as a plain append does not lose data, it invents it: the
    superseded halves survive and the answer appears twice."""
    found = read_session(session("splice.jsonl"))
    assert found.conversation is not None

    said = texts(found.conversation.messages[1])
    assert said == ["Thinking about it...", "A generator produces values lazily, one at a time."]


# --------------------------------------------------------------------------
# edges
# --------------------------------------------------------------------------


def test_a_hidden_turn_is_not_carried(session) -> None:  # type: ignore[no-untyped-def]
    """Copilot can hide a turn from its own display. Carrying it puts text in
    the transcript that the person never saw there -- the shape of the Codex
    bug that titled every conversation `<permissions instructions>`."""
    found = read_session(session("edge.jsonl"))
    assert found.conversation is not None

    every = " ".join(t for m in found.conversation.messages for t in texts(m))
    assert "Invisible." not in every
    assert any("hidden" in note for note in found.notes)


def test_an_empty_thinking_block_is_reported_not_invented(session) -> None:  # type: ignore[no-untyped-def]
    """Every thinking block on the probe machine was empty: Copilot writes the
    marker without the reasoning. An empty ThinkingBlock would be a lie."""
    found = read_session(session("edge.jsonl"))
    assert found.conversation is not None

    thinking = [b for m in found.conversation.messages for b in m.content]
    assert not any(isinstance(b, ThinkingBlock) for b in thinking)
    assert found.dropped_kinds.get("thinking (no text stored)") == 2


def test_an_unknown_delta_kind_is_warned_about_not_fatal(session) -> None:  # type: ignore[no-untyped-def]
    found = read_session(session("edge.jsonl"))

    assert found.conversation is not None
    assert any("kind 7" in w for w in found.warnings)


def test_a_corrupt_line_is_warned_about_not_fatal(session) -> None:  # type: ignore[no-untyped-def]
    found = read_session(session("edge.jsonl"))

    assert found.conversation is not None
    assert any("unreadable lines" in w for w in found.warnings)


def test_an_unknown_response_kind_is_counted(session) -> None:  # type: ignore[no-untyped-def]
    found = read_session(session("edge.jsonl"))

    assert found.dropped_kinds.get("somethingNew") == 1


def test_a_pasted_image_travels_as_bytes(session) -> None:  # type: ignore[no-untyped-def]
    """Copilot stores these inline as base64, so nothing has to be found on
    disk -- unlike Codex, where the file is named only in message prose."""
    found = read_session(session("edge.jsonl"))
    assert found.conversation is not None

    assert len(found.images) == 1
    image = found.images[0]
    assert image.data[:4] == b"\x89PNG"
    assert image.record.mime_type == "image/png"
    blocks = [b for m in found.conversation.messages for b in m.content]
    assert any(isinstance(b, ImageBlock) and b.attachment_id == image.record.id for b in blocks)


def test_a_chat_that_was_opened_and_never_used_yields_nothing(session) -> None:  # type: ignore[no-untyped-def]
    """VS Code writes one of these every time a panel opens. It is the common
    case, not a fault, and it must not become an empty conversation."""
    found = read_session(session("empty.jsonl"))

    assert found.conversation is None
    assert "no messages" in found.notes


def test_a_file_that_is_not_named_for_a_conversation_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "not-a-uuid.jsonl"
    path.write_text("{}", encoding="utf-8")

    found = read_session(path)

    assert found.conversation is None


def test_a_title_falls_back_to_what_the_user_typed(tmp_path: Path) -> None:
    """Copilot's own generated summary is not stored, so the first typed line
    is the best available name -- better than no name at all."""
    path = tmp_path / f"{SESSION_ID}.jsonl"
    records = [
        json.loads((FIXTURES / "empty.jsonl").read_text(encoding="utf-8")),
        {
            "kind": 2,
            "k": ["requests"],
            "v": [
                {
                    "requestId": "r1",
                    "message": {"text": "  What   does this   do?  ", "parts": []},
                    "response": [{"value": "It does things.", "supportHtml": False}],
                }
            ],
        },
    ]
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8", newline="\n"
    )

    found = read_session(path)

    assert found.conversation is not None
    assert found.conversation.title == "What does this do?"
