"""What Antigravity's field numbers mean, and where each was proved.

Nothing here was guessed from a field's size or position. Every mapping was
derived by joining ``brain/<uuid>/.system_generated/logs/transcript.jsonl`` --
which is plain JSON and names its step types in words -- to ``steps.idx``
through ``step_index``. Every mapping below was verified that way, and the
counts in the comments are how many real steps agreed.

Two things about that oracle shape the code:

* **``step_index`` is many-to-one.** One step can emit several transcript
  lines, so a mapping that assumes one line per step reports conflicts that
  are not conflicts.
* **The transcript's ``content`` is a rendering, not the stored string.** It is
  routinely *longer* than the field it came from, so a mapping must be checked
  by containment in both directions. Checking one direction found nothing and
  nearly produced a confident, wrong claim about the user's data.

``step_payload`` is a oneof: field 5 carries metadata common to every step, and
each step type puts its own content at its own field number -- USER_INPUT at
19, PLANNER_RESPONSE at 20, RUN_COMMAND at 56, and so on. That is why the paths
below share no prefix.
"""

from __future__ import annotations

from typing import Final

from ferry.ucs import Role

__all__ = [
    "CHECKPOINT",
    "PARENT_CONVERSATION",
    "ROOT_CONVERSATION",
    "PLANNER_RESPONSE",
    "STEP_TIMESTAMP",
    "STEP_TYPES",
    "TEXT_FIELDS",
    "TOOL_TYPES",
    "USER_INPUT",
    "role_of",
    "type_name",
]

USER_INPUT: Final = 14
PLANNER_RESPONSE: Final = 15
CHECKPOINT: Final = 23

STEP_TYPES: Final[dict[int, str]] = {
    5: "CODE_ACTION",
    7: "GREP_SEARCH",
    8: "VIEW_FILE",
    9: "LIST_DIRECTORY",
    14: "USER_INPUT",
    15: "PLANNER_RESPONSE",
    17: "ERROR_MESSAGE",
    21: "RUN_COMMAND",
    23: "CHECKPOINT",
    31: "READ_URL_CONTENT",
    33: "SEARCH_WEB",
    91: "GENERATE_IMAGE",
    98: "CONVERSATION_HISTORY",
    101: "SYSTEM_MESSAGE",
    127: "INVOKE_SUBAGENT",
    132: "GENERIC",
    138: "ASK_QUESTION",
}
"""``steps.step_type`` to the name the transcript gives it.

**Type 28 is deliberately absent.** It occurs 12 times in the databases and
never once in the transcript, so there is no evidence for what it is. It reads
as an unknown step rather than being given a plausible-sounding name, because
a wrong name in this table would be indistinguishable from a right one
everywhere else in the codebase.
"""

PARENT_CONVERSATION: Final = (5,)
"""Field path in ``trajectory_metadata_blob`` naming the conversation that spawned this one.

Present only in a subagent trajectory. **This is the difference between what
Ferry sees on disk and what Antigravity shows you.** When the agent spawns a
subagent, that subagent gets its own database in ``conversations/``, looking
exactly like a conversation -- but the app never lists it, because it is part
of the conversation it came from.

On the probe machine that is 6 databases for the 2 conversations the user
sees: three subagents under one, one under the other. Counting databases would
have reported three times as many conversations as exist, which is the kind of
wrong that makes a person distrust every other number the tool prints.

Corroborated by :data:`ROOT_CONVERSATION`, which agrees on all six.
"""

ROOT_CONVERSATION: Final = (6,)
"""Field path naming the conversation at the top of the tree.

A top-level conversation names *itself* here; a subagent names its parent. So
it says the same thing as :data:`PARENT_CONVERSATION` from the other
direction, and the two are checked against each other rather than either being
trusted alone -- the whole count depends on this being right.
"""

STEP_TIMESTAMP: Final = (1, 1)
"""Field path of a step's time, in ``steps.metadata``, epoch seconds.

Present in 2,725 of 2,725 steps, and agreeing with the transcript's
``created_at`` in 2,687 of 2,692 comparable ones. The five that differ are
about a minute apart -- a step whose transcript line was written when the work
started rather than when it finished.
"""

TEXT_FIELDS: Final[dict[int, tuple[tuple[int, ...], ...]]] = {
    5: ((10, 2, 1, 2, 3, 3, 1), (10, 1, 2, 2, 5)),
    7: ((13, 4, 3),),
    8: ((14, 1),),
    9: ((15, 3, 1),),
    14: ((19, 2), (19, 3, 1)),
    15: ((20, 1), (20, 8)),
    17: ((24, 3, 9), (24, 3, 2)),
    21: ((56, 21, 1, 1), (133, 2, 5)),
    23: ((30, 19), (30, 14, 1)),
    31: ((56, 21, 1, 2),),
    33: ((42, 1),),
    91: ((104, 1),),
    101: ((114, 4, 3), (114, 1)),
    127: ((143, 10, 1),),
    132: ((140, 1, 2), (140, 2, 1)),
    138: ((56, 22, 1, 2, 2), (154, 1, 2, 2)),
}
"""Where each step type keeps its text in ``step_payload``, best first.

Ordered, and read in order, because several types carry the same text twice --
PLANNER_RESPONSE has it at both 20.1 and 20.8 in all 406 steps that have any --
and because the second entry is often the one that survives when the first is
absent.

USER_INPUT is the case worth knowing about. **19.2 is what the user typed**
(118 of 118), while 19.3.1 matches more steps (134) because it is the fuller
rendering, context included. Ferry takes 19.2: the message a user wrote is what
belongs in their history, and the surrounding context is the tool's, not
theirs.
"""

TOOL_TYPES: Final[frozenset[int]] = frozenset({5, 7, 8, 9, 21, 31, 33, 91, 127})
"""Step types that are the agent doing something rather than saying something."""

_ROLES: Final[dict[int, Role]] = {
    14: "user",
    15: "assistant",
    138: "assistant",
    98: "system",
    101: "system",
    17: "system",
    132: "assistant",
}


def type_name(step_type: int) -> str:
    """The name of a step type, or a marker naming the number it did not know."""
    return STEP_TYPES.get(step_type, f"UNKNOWN_{step_type}")


def role_of(step_type: int) -> Role:
    """Which UCS role a step type belongs to.

    Anything the agent *did* is a tool step; anything unrecognised is treated
    as the assistant's, which is where an unknown step is far likelier to
    belong than in the user's own words. Attributing a tool's output to the
    user would misrepresent their history, and that is the failure worth
    avoiding.
    """
    if step_type in TOOL_TYPES:
        return "tool"
    return _ROLES.get(step_type, "assistant")
