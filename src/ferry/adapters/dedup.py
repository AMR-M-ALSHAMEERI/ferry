"""Deciding whether a conversation already in the bundle is the right copy.

Every adapter skips a conversation whose id is already in the bundle. That is
what makes an interrupted export resumable, and it is correct as long as two
files with one id hold the same conversation.

They do not always. On the probe machine one Copilot conversation exists under
two workspaces in byte-different files, and while those two agreed, nothing
guaranteed it. Skipping on the id alone means that when they disagree the
bundle keeps whichever was read first -- and if that is the shorter one, turns
are lost with no error and no warning.

So the skip checks. It costs a re-read, paid only when a duplicate is actually
found, which on real data is close to never.

**This is shared rather than per-adapter deliberately:** the skip-by-id pattern
is in all three adapters, so the risk is in all three, and one of them having a
guard the others lack is how the next adapter inherits the fault.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ferry.ucs import Conversation

__all__ = ["ALREADY_THERE", "DuplicateVerdict", "compare_duplicate"]

ALREADY_THERE = "already in bundle"
"""How a skip says *this one was carried on an earlier run*.

The export screen reads it to tell a resume apart from a conversation that
could not be carried. Both are skips; only one of them is good news.
"""


@dataclass(frozen=True)
class DuplicateVerdict:
    """What to say about a conversation that is already in the bundle."""

    message: str
    """The text for the ``skipped`` event."""

    warning: str | None = None
    """Set when the copy being skipped holds more than the bundle's."""


def compare_duplicate(
    conversation_id: UUID,
    incoming: Conversation | None,
    existing: Conversation | None,
    source_name: str,
) -> DuplicateVerdict:
    """Compare the copy on disk with the one already carried.

    Args:
        conversation_id: The id both copies claim.
        incoming: The conversation the source file holds, or ``None`` if it
            could not be read -- in which case nothing can be compared and the
            skip stands unchanged.
        existing: The conversation already in the bundle, same caveat.
        source_name: What to call the file in a warning.

    Returns:
        The message for the skip, and a warning when the skipped copy is
        longer. Only *longer* is reported: a shorter or equal copy is exactly
        what a duplicate is expected to be, and warning about those would put
        noise in front of the user on every resumed export.
    """
    plain = DuplicateVerdict(ALREADY_THERE)
    if incoming is None or existing is None:
        return plain
    if len(incoming.messages) <= len(existing.messages):
        return plain

    return DuplicateVerdict(
        message=f"{ALREADY_THERE} (a longer copy exists - see warning)",
        warning=(
            f"{source_name} holds {len(incoming.messages)} messages but the bundle already "
            f"has this conversation with {len(existing.messages)}; the longer copy was not "
            "carried"
        ),
    )
