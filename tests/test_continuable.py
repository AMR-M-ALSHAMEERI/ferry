"""Continue mode: reducing a conversation to what another assistant can use.

Two properties carry this module, and they are different in kind:

- :class:`TestAPersonsWordsAreNeverMachinery` -- the transform may drop
  anything a tool produced and **nothing a person typed**. Asserted with the
  budget off, because the budget drops words on purpose and an assertion that
  conflated the two would have to be weakened to pass.
- :class:`TestWhatMakesItContinuable` -- no unsigned thinking block, and no
  tool call naming a tool the target does not have. These are the two reasons a
  migrated conversation could not be continued, and they must be impossible
  rather than unlikely.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from ferry.core.continuable import BUDGET, MAX_LINE, Flattened, continuable
from ferry.ucs import (
    Conversation,
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Workspace,
)


def conversation(*messages: Message, tool: str = "codex") -> Conversation:
    return Conversation(
        id=uuid4(),
        source_tool=tool,  # type: ignore[arg-type]
        title="A session",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        updated_at=datetime(2026, 8, 1, tzinfo=UTC),
        workspace=Workspace(name="Ferry", original_path="C:/Users/test/Ferry"),
        messages=list(messages),
    )


def user(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text=text)])


class TestAPersonsWordsAreNeverMachinery:
    """The invariant. Everything else here is a detail beside it."""

    def test_user_text_survives_word_for_word(self) -> None:
        typed = "Make the seal step atomic. I do not want a half-written bundle."
        item = conversation(
            user(typed),
            Message(
                role="assistant",
                content=[
                    ThinkingBlock(text="Checking the rename.", signature="vendor"),
                    ToolUseBlock(name="exec", input={"raw": "pytest -q"}, id="c1"),
                ],
            ),
            Message(role="tool", content=[ToolResultBlock(tool_use_id="c1", output="ok")]),
        )

        reduced, _ = continuable(item, budget=0)
        said = [
            block.text
            for message in reduced.messages
            for block in message.content
            if block.type == "text"
        ]

        assert typed in said

    def test_a_message_of_only_machinery_disappears_rather_than_emptying(self) -> None:
        """An empty turn is a turn the model has to interpret and cannot."""
        item = conversation(
            user("hello"),
            Message(role="assistant", content=[ThinkingBlock(text="hmm", signature="v")]),
        )

        reduced, gave_up = continuable(item, budget=0)

        assert len(reduced.messages) == 1
        assert gave_up.thinking == 1

    def test_the_original_conversation_is_not_touched(self) -> None:
        """The bundle stays lossless. This is what makes the mode safe."""
        item = conversation(
            user("hello"),
            Message(role="assistant", content=[ThinkingBlock(text="hmm", signature="v")]),
        )
        before = item.model_dump_json()

        continuable(item)

        assert item.model_dump_json() == before


class TestWhatMakesItContinuable:
    def test_no_thinking_block_survives_at_all(self) -> None:
        """Not "no unsigned block" -- none.

        An absent thinking block is accepted by the API; an invalid one is
        rejected outright and the conversation cannot be continued. Ferry
        writing ``"signature": ""`` is what this mode exists to stop, so the
        assertion is on the block, not on the signature.
        """
        item = conversation(
            Message(
                role="assistant",
                content=[
                    ThinkingBlock(text="signed", signature="vendor-issued"),
                    ThinkingBlock(text="unsigned"),
                    TextBlock(text="Done."),
                ],
            )
        )

        reduced, gave_up = continuable(item, budget=0)

        assert gave_up.thinking == 2
        assert not [b for m in reduced.messages for b in m.content if b.type == "thinking"]

    def test_no_tool_call_survives_as_a_tool_call(self) -> None:
        """A migrated Codex conversation must not tell Claude Code it ran
        ``apply_patch``. The target has no such tool and never did."""
        item = conversation(
            Message(
                role="assistant",
                content=[ToolUseBlock(name="apply_patch", input={"raw": "x"}, id="c1")],
            )
        )

        reduced, gave_up = continuable(item, budget=0)

        assert gave_up.calls == 1
        assert not [b for m in reduced.messages for b in m.content if b.type == "tool_use"]
        assert not [b for m in reduced.messages for b in m.content if b.type == "tool_result"]

    def test_a_call_becomes_a_line_naming_what_was_done(self) -> None:
        item = conversation(
            Message(
                role="assistant",
                content=[ToolUseBlock(name="exec", input={"raw": "pytest -q"}, id="c1")],
            )
        )

        reduced, _ = continuable(item, budget=0)
        line = reduced.messages[0].content[0]

        assert line.type == "text"
        assert line.text == "[ran] pytest -q"

    def test_an_unknown_tool_is_named_and_not_guessed_at(self) -> None:
        """The same rule Compact holds to: never a sentence nobody can source."""
        item = conversation(
            Message(
                role="assistant",
                content=[ToolUseBlock(name="some_future_tool", input={"a": 1}, id="c1")],
            )
        )

        reduced, _ = continuable(item, budget=0)

        assert reduced.messages[0].content[0].text == "[some_future_tool]"

    def test_a_failure_is_kept_because_it_is_why_the_next_thing_happened(self) -> None:
        item = conversation(
            Message(
                role="assistant",
                content=[ToolUseBlock(name="exec", input={"raw": "pytest"}, id="c1")],
            ),
            Message(
                role="tool",
                content=[
                    ToolResultBlock(
                        tool_use_id="c1",
                        output="Traceback (most recent call last):\nModuleNotFoundError: no tests",
                    )
                ],
            ),
        )

        reduced, _ = continuable(item, budget=0)
        said = " ".join(b.text for m in reduced.messages for b in m.content if b.type == "text")

        assert "[failed]" in said
        assert "ModuleNotFoundError" in said

    def test_a_successful_result_body_is_dropped(self) -> None:
        """79.3% of a conversation's bytes, and none of it is why anything
        happened next."""
        item = conversation(
            Message(
                role="tool",
                content=[ToolResultBlock(tool_use_id="c1", output="x" * 5000)],
            )
        )

        reduced, gave_up = continuable(item, budget=0)

        assert gave_up.results == 1
        assert reduced.messages == []

    def test_images_are_kept_because_a_person_chose_to_include_them(self) -> None:
        attachment = uuid4()
        item = conversation(Message(role="user", content=[ImageBlock(attachment_id=attachment)]))

        reduced, _ = continuable(item, budget=0)

        assert reduced.messages[0].content[0].type == "image"


class TestLinesAreClipped:
    def test_a_heredoc_does_not_become_the_transcript(self) -> None:
        """Measured, twice, and both measurements moved the limit down.

        With no limit the largest real conversation *grew* from 210,819 words to
        302,358. At Compact's 300 a Codex conversation still grew. A call line
        is navigation, not content.
        """
        item = conversation(
            Message(
                role="assistant",
                content=[ToolUseBlock(name="exec", input={"raw": "x" * 4000}, id="c1")],
            )
        )

        reduced, _ = continuable(item, budget=0)
        line = reduced.messages[0].content[0].text

        assert len(line) <= MAX_LINE
        assert line.endswith("...")


class TestTheBudget:
    def test_a_conversation_under_budget_is_untouched_by_it(self) -> None:
        item = conversation(user("short"), user("also short"))

        reduced, gave_up = continuable(item, budget=BUDGET)

        assert gave_up.dropped == 0
        assert len(reduced.messages) == 2

    def test_the_recent_end_is_what_survives(self) -> None:
        """What someone is carrying on from is the last thing that happened."""
        item = conversation(*[user(f"message {n} " + "word " * 100) for n in range(50)])

        reduced, gave_up = continuable(item, budget=500)
        said = " ".join(b.text for m in reduced.messages for b in m.content if b.type == "text")

        assert gave_up.dropped
        assert "message 49" in said
        assert "message 0 " not in said

    def test_what_was_cut_is_said_in_the_transcript(self) -> None:
        """Not only in the counts.

        Whoever picks this up must be able to see it starts partway through, or
        they read the first surviving turn as the beginning of the work.
        """
        item = conversation(*[user(f"message {n} " + "word " * 100) for n in range(50)])

        reduced, gave_up = continuable(item, budget=500)
        opening = reduced.messages[0].content[0].text

        assert "[Ferry]" in opening
        assert str(gave_up.dropped) in opening
        assert "still in the bundle" in opening

    def test_one_enormous_message_is_kept_whole_rather_than_cut_in_half(self) -> None:
        """A budget that keeps nothing is worse than one that overruns, and half
        a turn is a turn the model has to guess at."""
        item = conversation(user("word " * 5000))

        reduced, gave_up = continuable(item, budget=100)

        assert gave_up.dropped == 0
        assert len(reduced.messages[0].content[0].text.split()) == 5000

    def test_budget_zero_keeps_everything(self) -> None:
        item = conversation(*[user(f"message {n} " + "word " * 100) for n in range(50)])

        reduced, gave_up = continuable(item, budget=0)

        assert gave_up.dropped == 0
        assert len(reduced.messages) == 50


class TestWhatItReports:
    def test_nothing_given_up_reports_nothing_given_up(self) -> None:
        reduced, gave_up = continuable(conversation(user("hello")), budget=0)

        assert gave_up == Flattened()
        assert gave_up.anything is False

    @pytest.mark.parametrize("tool", ["claude-code", "codex", "copilot", "antigravity"])
    def test_it_works_for_every_source_tool(self, tool: str) -> None:
        item = conversation(
            user("hello"),
            Message(
                role="assistant",
                content=[ToolUseBlock(name="Bash", input={"command": "ls"}, id="c1")],
            ),
            tool=tool,
        )

        reduced, gave_up = continuable(item, budget=0)

        assert gave_up.calls == 1
        assert not [b for m in reduced.messages for b in m.content if b.type == "tool_use"]
