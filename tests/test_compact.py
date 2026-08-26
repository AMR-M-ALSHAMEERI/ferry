"""Compact: turning a conversation into a document without inventing anything.

The test that matters most here is :class:`TestTheQuotationProperty`. Every
other feature in Ferry can be checked by reading its output; this one makes a
claim -- *nothing in this document was made up* -- that a person cannot verify
by looking, because a fabricated sentence about their own conversation is
exactly the thing that reads as true. So it is asserted mechanically, over
every shape and length, on every conversation the tests can build.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import unquote
from uuid import uuid4

import pytest

from ferry.compact import compact, digest, quotations
from ferry.compact.catalogue import describe, fidelity
from ferry.compact.paste import trim
from ferry.compact.prune import error_line, facts, failed
from ferry.compact.rank import top
from ferry.compact.render import LENGTHS, SHAPES
from ferry.ucs import (
    Conversation,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Workspace,
)


def _strings(value: object) -> list[str]:
    """Every string inside a tool call's arguments or a result's payload.

    ``str(a_dict)`` is not the conversation -- it is Python's rendering of it,
    with newlines spelled as backslash-n. A command that really was two lines
    would never be found in it. Walking the structure compares what the
    conversation holds rather than how Python prints it.
    """
    if isinstance(value, str):
        # The decoded spelling counts as the same string. Copilot records a
        # file as `file:///c%3A/Users/...` and the catalogue renders it as
        # `c:/Users/...` -- the same path, re-spelled, exactly as re-wrapping
        # whitespace is the same sentence re-spelled. **These two are the only
        # transformations this comparison allows**, and both are reversible and
        # lossless. Anything else counts as invented, which is the point.
        decoded = unquote(value)
        return [value] if decoded == value else [value, decoded]
    if isinstance(value, dict):
        return [part for item in value.values() for part in _strings(item)]
    if isinstance(value, list | tuple):
        return [part for item in value for part in _strings(item)]
    return []


START = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
END = datetime(2026, 8, 26, 17, 30, tzinfo=UTC)


def conversation(
    *messages: Message, tool: str = "claude-code", title: str = "A session"
) -> Conversation:
    return Conversation(
        id=uuid4(),
        source_tool=tool,  # type: ignore[arg-type]
        title=title,
        created_at=START,
        updated_at=END,
        workspace=Workspace(name="Ferry", original_path="C:/Users/Dell/Desktop/Ferry"),
        messages=list(messages),
    )


def user(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text=text)])


def assistant(text: str) -> Message:
    return Message(role="assistant", content=[TextBlock(text=text)])


def call(name: str, arguments: dict, call_id: str = "c1") -> Message:  # type: ignore[type-arg]
    return Message(role="assistant", content=[ToolUseBlock(name=name, input=arguments, id=call_id)])


def result(output: object, call_id: str = "c1") -> Message:
    return Message(role="tool", content=[ToolResultBlock(tool_use_id=call_id, output=output)])


# --------------------------------------------------------------------------
# the catalogue
# --------------------------------------------------------------------------


class TestTheCatalogue:
    def test_claude_code_paths_and_commands_are_structured(self) -> None:
        assert describe("claude-code", "Edit", {"file_path": "a.py"}).paths == ("a.py",)
        assert describe("claude-code", "Edit", {"file_path": "a.py"}).kind == "write"
        assert describe("claude-code", "Read", {"file_path": "a.py"}).kind == "read"
        assert describe("claude-code", "Bash", {"command": "pytest -q"}).command == "pytest -q"

    def test_a_codex_patch_names_its_files_in_its_header(self) -> None:
        """A fixed vocabulary of four markers, counted over 341 real patches --
        this parses a format rather than guessing at prose."""
        patch = (
            "*** Begin Patch\n"
            "*** Update File: src/ferry/cli/flows.py\n"
            "@@ -1 +1 @@\n"
            "-old\n+new\n"
            "*** Add File: docs/NEW.md\n"
            "+hello\n"
            "*** End Patch\n"
        )
        found = describe("codex", "apply_patch", {"raw": patch})

        assert found.kind == "write"
        assert found.paths == ("src/ferry/cli/flows.py", "docs/NEW.md")

    def test_a_copilot_file_uri_becomes_a_path_a_person_recognises(self) -> None:
        found = describe(
            "copilot",
            "copilot_readFile",
            {"ferry_invocation_uris": ["file:///c%3A/Users/Dell/Desktop/Ferry/README.md"]},
        )

        assert found.paths == ("c:/Users/Dell/Desktop/Ferry/README.md",)

    def test_a_copilot_command_comes_from_what_was_asked_for(self) -> None:
        """`forDisplay` is what VS Code shortened to fit the screen. `original`
        is what ran."""
        found = describe(
            "copilot",
            "run_in_terminal",
            {"commandLine": {"original": "pytest -q --tb=short", "forDisplay": "pytest -q ..."}},
        )

        assert found.command == "pytest -q --tb=short"

    def test_antigravity_finds_the_path_inside_the_sentence(self) -> None:
        """The whole milestone turned on this. The first spike anchored its
        pattern, insisted the whole string be a path, and reported that
        VIEW_FILE almost never names a file. Searching inside the same strings
        found one in 115 of 115."""
        found = describe(
            "antigravity",
            "VIEW_FILE",
            {"detail": "Analyzed C:\\Users\\Dell\\Desktop\\Ferry\\src\\ferry\\cli\\flows.py"},
        )

        assert found.kind == "read"
        assert found.paths == ("C:\\Users\\Dell\\Desktop\\Ferry\\src\\ferry\\cli\\flows.py",)

    def test_antigravity_offers_no_command_because_it_records_none(self) -> None:
        """Measured at 34% purity -- a bare binary name, not the line that was
        typed. An empty command is the honest answer."""
        found = describe("antigravity", "RUN_COMMAND", {"detail": "git"})

        assert found.kind == "command"
        assert found.command == ""

    def test_an_unknown_tool_is_ordinary_not_an_error(self) -> None:
        """Forty-one distinct tool names appeared across nineteen real
        conversations, and every release of every tool adds more."""
        assert describe("claude-code", "SomeToolShippedNextYear", {"x": 1}).kind == "other"
        assert describe("a-tool-ferry-has-never-heard-of", "Read", {}).kind == "other"

    def test_a_missing_argument_yields_nothing_rather_than_a_guess(self) -> None:
        assert describe("claude-code", "Edit", {}).paths == ()
        assert describe("claude-code", "Bash", None).command == ""

    def test_every_tool_states_what_its_record_is_worth(self) -> None:
        assert fidelity("claude-code").tier == "complete"
        assert fidelity("antigravity").tier == "partial"
        assert fidelity("antigravity").commands
        assert fidelity("copilot").files
        assert fidelity("something-else").tier == "unknown"


# --------------------------------------------------------------------------
# pruning
# --------------------------------------------------------------------------


class TestFailureIsNotGuessedAt:
    def test_a_tool_that_says_it_failed_is_believed(self) -> None:
        assert failed({"isError": True}) is True
        assert failed({"isError": False}) is False
        assert failed({"Err": "nope"}) is True
        assert failed({"Ok": "fine"}) is False

    def test_an_exception_is_a_failure(self) -> None:
        assert failed("ModuleNotFoundError: No module named 'tests'") is True
        assert failed("Traceback (most recent call last):\n  File 'x'") is True

    def test_a_test_run_reporting_failures_is_not_a_failed_call(self) -> None:
        """The single most important line in this module. The word "failed"
        appears in 15% of real Codex results and 9.8% of Claude Code's, almost
        always because pytest said so -- which is the tool working."""
        assert failed("3 failed, 884 passed in 116.36s") is False
        assert failed("FAILURES ahead: the suite reports two failures") is False

    def test_a_result_with_nothing_in_it_says_nothing(self) -> None:
        """None is not False. A conversation where no tool reported an outcome
        should not claim everything succeeded."""
        assert failed("") is None
        assert failed("   \n ") is None
        assert failed(None) is None

    def test_the_error_line_is_the_one_that_names_the_error(self) -> None:
        text = "running the suite\nlots of output\nImportError: cannot import name 'x'\nmore"

        assert error_line(text) == "ImportError: cannot import name 'x'"

    def test_a_thousand_character_traceback_line_is_cut_not_quoted_whole(self) -> None:
        line = error_line("ValueError: " + "x" * 900)

        assert len(line) <= 200
        assert line.endswith("...")

    def test_a_call_is_paired_with_its_result(self) -> None:
        found = facts(
            conversation(
                call("Bash", {"command": "pytest -q"}, "c1"),
                result("ImportError: no module named 'tests'", "c1"),
            )
        )

        assert len(found) == 1
        assert found[0].name == "Bash"
        assert found[0].ok is False
        assert found[0].error == "ImportError: no module named 'tests'"

    def test_a_result_that_names_no_call_still_counts_its_error(self) -> None:
        """255 of 6,442 real results name no call. Dropping them would hide
        real failures for the sake of a tidy table."""
        found = facts(conversation(result({"Err": "PermissionError: Access is denied"}, "nobody")))

        assert [f.error for f in found] == ["PermissionError: Access is denied"]


# --------------------------------------------------------------------------
# a person's words
# --------------------------------------------------------------------------


class TestTellingWritingFromPasting:
    def test_a_long_instruction_is_kept_whole(self) -> None:
        """Length is the tempting signal and the wrong one. A long message is
        as likely to be dense intent as it is to be a log."""
        text = (
            "Go ahead with option a first and add b later as another option for "
            "convenience, but a has to exist first. Do not touch the other adapters "
            "while you are in there, and update the progress file at the end of the "
            "session no matter how far we get. I would rather have three finished "
            "things than six half-finished ones."
        )

        found = trim(text)

        assert found.text == text
        assert found.removed == 0

    def test_a_pasted_traceback_collapses_to_its_error(self) -> None:
        text = (
            "what is this?\n"
            "Traceback (most recent call last):\n"
            '  File "run.py", line 3, in <module>\n'
            '  File "lib.py", line 9, in load\n'
            '  File "lib.py", line 4, in read\n'
            "ImportError: cannot import name 'x'\n"
        )

        found = trim(text)

        assert found.text == "what is this?"
        assert found.removed >= 3
        assert found.error == "ImportError: cannot import name 'x'"

    def test_a_fenced_block_comes_out(self) -> None:
        found = trim('here is the config\n```json\n{"a": 1}\n```\nwhy does it fail?')

        assert found.text == "here is the config\nwhy does it fail?"
        assert found.removed == 3

    def test_a_message_that_is_only_a_paste_says_so(self) -> None:
        found = trim(
            "Traceback (most recent call last):\n"
            '  File "a.py", line 1\n'
            '  File "b.py", line 2\n'
            "ValueError: bad\n"
        )

        assert found.is_paste
        assert found.error == "ValueError: bad"

    def test_one_quoted_line_inside_a_sentence_is_not_a_paste(self) -> None:
        """Three in a row make a paste, because two is a coincidence -- people
        quote a line of an error mid-sentence, and taking it away edits what
        they said."""
        text = 'I keep seeing File "x.py", line 3 in the output, is that normal?'

        assert trim(text).text == text


# --------------------------------------------------------------------------
# ranking
# --------------------------------------------------------------------------


class TestRanking:
    def test_sentences_come_back_in_the_order_they_were_said(self) -> None:
        pool = [
            "The cache was the first suspect and it was not the cache at all.",
            "Every single one of these sentences is padding with nothing in it.",
            "The fix is in `src/ferry/core/sealed.py`, because the salt was reused.",
        ]

        chosen = top(pool, limit=2)

        assert list(chosen) == [line for line in pool if line in chosen]

    def test_nothing_is_written_only_chosen(self) -> None:
        pool = ["One sentence about the parser.", "Another sentence about the parser entirely."]

        for line in top(pool, limit=1):
            assert line in " ".join(pool)

    def test_asking_for_nothing_returns_nothing(self) -> None:
        assert top(["anything at all here"], limit=0) == ()
        assert top([], limit=5) == ()

    def test_the_same_pool_ranks_the_same_way_twice(self) -> None:
        pool = [f"A sentence numbered {n} about parsers and caches." for n in range(40)]

        assert top(pool, limit=5) == top(pool, limit=5)


# --------------------------------------------------------------------------
# the document
# --------------------------------------------------------------------------


@pytest.fixture
def realistic() -> Conversation:
    """A conversation with one of everything the renderer can meet."""
    return conversation(
        user("Can you make the seal step atomic? I do not want a half-written bundle."),
        assistant("I will write beside the original and rename it into place."),
        call("Edit", {"file_path": "src/ferry/core/sealed.py"}, "c1"),
        result("edited", "c1"),
        call("Edit", {"file_path": "src/ferry/core/sealed.py"}, "c2"),
        result("edited", "c2"),
        call("Read", {"file_path": "tests/test_sealed.py"}, "c3"),
        result("read 40 lines", "c3"),
        call("Bash", {"command": "pytest -q"}, "c4"),
        result("ModuleNotFoundError: No module named 'tests'", "c4"),
        assistant("That is the import path, not the change. pytest and python -m pytest differ."),
        call("Bash", {"command": "pytest -q"}, "c5"),
        result("887 passed", "c5"),
        Message(
            role="assistant",
            content=[
                ThinkingBlock(text="Maybe it is the cache. No, it is the working directory."),
                TextBlock(text="Green. The rename is atomic on the same filesystem."),
            ],
        ),
        user("Good. Leave the clipboard part for later, we can come back to it."),
        assistant("Understood. The reseal is done and the clipboard is untouched."),
    )


class TestTheQuotationProperty:
    """The test that makes "nothing was invented" a fact rather than a hope."""

    @staticmethod
    def source_text(item: Conversation) -> str:
        """Everything the conversation contains, as one flat run of words.

        Whitespace is collapsed on both sides of the comparison. A quotation is
        the same words in the same order; a document that preserved the source's
        line wrapping would be unreadable, and re-wrapping is not inventing.
        """
        parts: list[str] = []
        for message in item.messages:
            for block in message.content:
                if block.type in ("text", "thinking"):
                    parts.append(block.text)
                elif block.type == "tool_use":
                    parts.extend(_strings(block.input))
                elif block.type == "tool_result":
                    parts.extend(_strings(block.output))
        return " ".join(" ".join(parts).split())

    @pytest.mark.parametrize("shape", SHAPES)
    @pytest.mark.parametrize("length", sorted(LENGTHS))
    def test_every_quotation_really_is_one(
        self, realistic: Conversation, shape: str, length: str
    ) -> None:
        document = compact(realistic, shape=shape, length=length)
        source = self.source_text(realistic)

        for quotation in quotations(document):
            wanted = " ".join(quotation.split())
            if wanted.endswith("..."):
                wanted = wanted[:-3]
            assert wanted in source, f"not in the conversation: {quotation!r}"

    def test_it_holds_when_the_conversation_is_nothing_but_tools(self) -> None:
        item = conversation(
            call("Bash", {"command": "ls"}, "c1"),
            result("a b c", "c1"),
        )
        document = compact(item)

        for quotation in quotations(document):
            assert " ".join(quotation.split()) in self.source_text(item)


class TestTheDocument:
    def test_the_same_conversation_renders_the_same_bytes(self, realistic: Conversation) -> None:
        assert compact(realistic) == compact(realistic)

    def test_a_file_edited_twice_outranks_one_read_once(self, realistic: Conversation) -> None:
        found = digest(realistic)

        assert found.files[0].path == "src/ferry/core/sealed.py"
        assert found.files[0].edited == 2
        assert found.files[0].read == 0

    def test_a_command_is_counted_with_its_failures(self, realistic: Conversation) -> None:
        found = digest(realistic)

        assert found.commands[0].text == "pytest -q"
        assert found.commands[0].times == 2
        assert found.commands[0].failed == 1

    def test_thinking_never_reaches_the_document(self, realistic: Conversation) -> None:
        """A thinking block is a draft, and its dead ends read as established
        fact to whoever pastes this into a new session. That is the
        hallucination problem this feature avoids, coming back in by the back
        door."""
        document = compact(realistic, length="full")

        assert "Maybe it is the cache" not in document

    def test_what_the_user_said_is_present_word_for_word(self, realistic: Conversation) -> None:
        document = compact(realistic)

        assert "I do not want a half-written bundle." in document

    def test_a_thing_left_for_later_is_listed_as_still_open(self, realistic: Conversation) -> None:
        document = compact(realistic)

        assert "## Still open" in document
        assert "Leave the clipboard part for later" in document

    def test_an_empty_section_is_left_out_entirely(self) -> None:
        document = compact(conversation(user("hello"), assistant("hi")))

        assert "## Files touched" not in document
        assert "## Commands run" not in document
        assert "## Errors seen" not in document

    def test_a_tool_that_cannot_yield_a_ledger_says_so_instead_of_none(self) -> None:
        """ "Commands run: none" for a session that ran forty would be a lie
        told by omission."""
        item = conversation(
            user("run the tests"),
            call("RUN_COMMAND", {"detail": "git"}, "c1"),
            tool="antigravity",
        )
        document = compact(item)

        assert "## Commands run" in document
        assert "Antigravity records the shell and the program name" in document

    def test_a_conversation_with_no_messages_still_produces_a_document(self) -> None:
        document = compact(conversation())

        assert document.startswith("# Compact:")
        assert document.endswith("\n")

    def test_the_footer_says_what_the_document_is(self, realistic: Conversation) -> None:
        document = compact(realistic)

        assert "without sending them anywhere" in document
        assert "quoted from the conversation or counted from it" in document

    def test_only_the_users_words_appear_in_the_said_shape(self, realistic: Conversation) -> None:
        document = compact(realistic, shape="said")

        assert "## What you said" in document
        assert "## Files touched" not in document
        assert "## Commands run" not in document

    def test_only_the_work_appears_in_the_done_shape(self, realistic: Conversation) -> None:
        document = compact(realistic, shape="done")

        assert "## What you said" not in document
        assert "## Files touched" in document

    def test_brief_is_shorter_than_full(self, realistic: Conversation) -> None:
        brief = compact(realistic, length="brief")
        full = compact(realistic, length="full")

        assert len(brief) <= len(full)


class TestCompression:
    def test_a_realistic_conversation_compacts_by_ninety_percent(self) -> None:
        """Ninety-one percent of a conversation is tool bodies. The target is
        met by deleting them, before any ranking runs at all."""
        messages: list[Message] = [user("Please fix the parser.")]
        for n in range(60):
            messages.append(assistant(f"Looking at step {n} of the parser now."))
            messages.append(call("Read", {"file_path": f"src/module_{n}.py"}, f"c{n}"))
            messages.append(result("x" * 4000, f"c{n}"))

        item = conversation(*messages)
        original = len(item.model_dump_json())
        document = compact(item)

        assert len(document) < original * 0.10


class TestQuotingAcrossAGap:
    """Three defects the self-check found on real data that no unit test had.

    All three are the same mistake in different clothes: text that is adjacent
    in Ferry's working copy was **not** adjacent in the conversation, and
    quoting across the join produces a sentence nobody said. It is the exact
    failure mode this feature exists to avoid, arrived at by accident rather
    than by hallucination.
    """

    def test_a_fragment_is_never_taken_across_a_removed_paste(self) -> None:
        """Found as a 297-character opening quotation from a Codex session that
        was nowhere in the session."""
        message = (
            "I ran the thing and it broke, here is what came out:\n"
            "Traceback (most recent call last):\n"
            '  File "a.py", line 1\n'
            '  File "b.py", line 2\n'
            "ValueError: bad\n"
            "so I will come back to this later"
        )
        found = trim(message)

        assert len(found.segments) == 2
        assert found.segments[0] == "I ran the thing and it broke, here is what came out:"
        assert found.segments[1] == "so I will come back to this later"
        # Joined for display, but no fragment is ever taken from the join.
        assert "\n" in found.text

    def test_open_threads_come_from_one_side_of_the_gap_or_the_other(self) -> None:
        item = conversation(
            user(
                "start here\n"
                "Traceback (most recent call last):\n"
                '  File "a.py", line 1\n'
                '  File "b.py", line 2\n'
                "ValueError: bad\n"
                "leave the clipboard for later"
            )
        )
        source = TestTheQuotationProperty.source_text(item)

        for quotation in quotations(compact(item)):
            assert " ".join(quotation.split()).removesuffix("...") in source

    def test_two_prose_blocks_with_a_tool_call_between_them_are_not_glued(self) -> None:
        """Found as an 811-character quotation from a Copilot session. Copilot
        puts a tool call between two text blocks of one message, and names a
        file as a text block of its own."""
        item = conversation(
            user("what does this do?"),
            Message(
                role="assistant",
                content=[
                    TextBlock(text="Let me look at the file and see what it says."),
                    ToolUseBlock(name="Read", input={"file_path": "a.py"}, id="c1"),
                    TextBlock(text="It defines one function and nothing else at all."),
                ],
            ),
            tool="copilot",
        )
        source = TestTheQuotationProperty.source_text(item)

        for quotation in quotations(compact(item, length="full")):
            assert " ".join(quotation.split()).removesuffix("...") in source


class TestNotClaimingMoreThanTheToolRecorded:
    def test_antigravity_says_its_file_list_is_reads_only(self) -> None:
        """Antigravity records what it viewed, at 100%. What it *changed* is
        measurable only at 8.6% purity, so Ferry does not extract it -- and a
        header reading "0 files touched" for a session that edited twenty would
        be a false summary in the document's first line."""
        item = conversation(
            user("build the popup"),
            call("CODE_ACTION", {"detail": "Edited the file"}, "c1"),
            tool="antigravity",
        )
        document = compact(item)

        assert "0 files read" in document
        assert "0 files touched" not in document
        assert "not in its record" in document

    def test_claude_code_says_touched_because_it_records_both(self) -> None:
        item = conversation(
            user("fix it"),
            call("Edit", {"file_path": "a.py"}, "c1"),
        )
        document = compact(item)

        assert "1 file touched" in document
