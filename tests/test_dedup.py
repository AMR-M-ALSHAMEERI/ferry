"""Deciding whether a duplicate conversation id is safe to skip.

Skipping by id is what makes an interrupted export resumable. It is correct
while two files with one id hold the same conversation -- and silently wrong
when they do not, because the bundle keeps whichever was read first.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from ferry.adapters.dedup import compare_duplicate
from ferry.ucs import Conversation, Message, TextBlock, Workspace

CONV = UUID("11111111-1111-4111-8111-111111111111")


def conversation(messages: int) -> Conversation:
    return Conversation(
        id=CONV,
        source_tool="copilot",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        workspace=Workspace(),
        messages=[Message(role="user", content=[TextBlock(text=f"m{i}")]) for i in range(messages)],
    )


def test_an_identical_duplicate_is_skipped_without_noise() -> None:
    """The normal case, and the one a resumed export hits constantly. Warning
    about it would put a scare in front of every resume."""
    verdict = compare_duplicate(CONV, conversation(4), conversation(4), "a.jsonl")

    assert verdict.warning is None
    assert verdict.message == "already in bundle"


def test_a_shorter_duplicate_is_skipped_without_noise() -> None:
    verdict = compare_duplicate(CONV, conversation(2), conversation(4), "a.jsonl")

    assert verdict.warning is None


def test_a_longer_duplicate_is_reported() -> None:
    """The case that loses data: the bundle keeps the shorter conversation and
    nothing says so."""
    verdict = compare_duplicate(CONV, conversation(9), conversation(4), "a.jsonl")

    assert verdict.warning is not None
    assert "9 messages" in verdict.warning
    assert "with 4" in verdict.warning
    assert "a.jsonl" in verdict.warning
    assert "see warning" in verdict.message


def test_an_unreadable_copy_leaves_the_skip_unchanged() -> None:
    """Nothing can be compared, so nothing is claimed. Guessing here would
    either invent a warning or suppress a real one."""
    assert compare_duplicate(CONV, None, conversation(4), "a.jsonl").warning is None
    assert compare_duplicate(CONV, conversation(4), None, "a.jsonl").warning is None
    assert compare_duplicate(CONV, None, None, "a.jsonl").message == "already in bundle"
