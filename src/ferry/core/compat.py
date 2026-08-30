"""Which conversation can be written into which tool, and what it costs.

Cross-tool migration is the adapter contract working as designed rather than a
new subsystem: every adapter exports to UCS and every adapter imports from UCS,
and :meth:`Adapter.import_` has never required that the UCS came from the same
tool. What this module adds is the part that is not free -- **knowing which
pairs actually work, and saying what is lost before anything is written.**

**The table below is measured, not assumed.** Every ordered pair was tried with
a real conversation from the human's own machine
(``spikes/probe_cross_tool.py``): imported into a scratch store, then exported
back out again, because *the import reported success* and *the conversation is
really there* are different claims and only the second is worth shipping.

======================  ==================================================
``-> claude-code``      **supported.** All three other tools, every
                        message, 100% of the words read back.
``-> codex``            unsupported. Its importer needs a ``session_meta``
                        header out of ``source_raw``; a foreign bundle
                        has not got one.
``-> copilot``          unsupported **today**, and the nearest to working.
                        A conversation is stored as a VS Code transcript
                        document and Ferry can only write back one it read.
                        The chat index is *not* the obstacle: Ferry writes it
                        already and every field in it has an obvious default.
``-> antigravity``      unsupported. A conversation is restored *from its
                        original SQLite database*. Ferry can copy one; it
                        cannot generate one.
======================  ==================================================

**Unsupported is an answer, not a failure.** PLAN section 5 M7b permits
Antigravity-as-target to end the milestone this way, and the honest ceiling for
every pair that does work is a *readable transcript* in the target tool -- not a
session the target's AI can meaningfully continue.

**A reason here must be a measured one.** The first version of this table said
Copilot was refused because the workspace-key derivation was unsolved. That was
taken from the PLAN's original feasibility assessment and had been **false for
six days** -- PROGRESS #124 solved the derivation, #130 verified it against
folders it had never seen, and ``copilot/paths.py`` implements it. The refusal
was right and the explanation was invented, which no test can catch. PLAN.md
records what was expected; PROGRESS records what was found; where they
disagree, PROGRESS wins.

**It happened twice.** The replacement blamed the chat index, which was true in
the narrow sense that Ferry does not build one for a foreign conversation, and
misleading about why: the index is the easy half. Measuring the real documents
(#203) showed the hard half is the transcript itself, and that an index entry
can be built from nothing but a title and a timestamp. A reason that points at
the wrong obstacle sends the next person to fix the wrong thing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ferry.ucs import Conversation

__all__ = [
    "CEILING",
    "Loss",
    "Pair",
    "Support",
    "assess",
    "pair",
    "refusal",
]

Support = Literal["native", "supported", "experimental", "unsupported"]
"""``native`` is the same tool at both ends -- not a migration at all, and the
only case that needs no permission. The other three are ordered by how much of
the conversation survives, and only ``unsupported`` is refused outright."""

CEILING = (
    "A converted conversation is a readable transcript in the target tool, "
    "not a session its assistant can pick up and continue."
)
"""The claim Ferry is willing to make, stated wherever a conversion is offered.

PLAN section 1's honest-scope note, only more so. Said in the CLI, the README
and ``docs/CROSS-TOOL.md``, because a person deciding whether to convert their
history deserves to know what they will get before they spend an evening on it.
"""


@dataclass(frozen=True)
class Pair:
    """What one ordered ``(source, target)`` pair is worth."""

    support: Support
    reason: str = ""
    """Why, in a sentence a person can act on. Required for anything that is
    not ``supported`` or ``native``; empty otherwise."""


_TARGETS: dict[str, Pair] = {
    "claude-code": Pair("supported"),
    "codex": Pair(
        "unsupported",
        "Codex rebuilds a conversation from the session_meta header in its own "
        "rollout file, and a bundle from another tool does not carry one.",
    ),
    "copilot": Pair(
        "unsupported",
        "Copilot Chat stores a conversation as a VS Code transcript document, "
        "and Ferry can only write back one it read. It cannot yet build one "
        "for a conversation that came from another tool.",
    ),
    "antigravity": Pair(
        "unsupported",
        "Antigravity conversations are restored from the original SQLite "
        "database they were exported with. Ferry can carry one across; it "
        "cannot build one for a conversation that never had it.",
    ),
}
"""Keyed on the **target** alone.

Every measured refusal turned out to be a property of the target's storage
rather than of the pair: what blocks a Codex import blocks it identically
whichever tool the conversation came from. Keyed on the pair, this table would
have twelve rows saying four things, and four of them would be the ones that
went stale. If a future source ever changes that, this becomes a dict of dicts
and :func:`pair` is the only caller that has to notice.
"""


def pair(source: str, target: str) -> Pair:
    """What writing a ``source`` conversation into ``target`` is worth.

    Plain strings rather than ``ToolName``: ``Adapter.name`` is a ``str`` by the
    base contract, and every caller is asking on behalf of an adapter. An
    unrecognised name is not an error here -- it already has an answer, and it
    is "no".
    """
    if source == target:
        return Pair("native")
    return _TARGETS.get(target, Pair("unsupported", f"Ferry has no adapter for {target}."))


def refusal(source: str, target: str) -> str:
    """Why this pair cannot be written, or ``""`` when it can.

    The one place the CLI and every adapter ask the question, so a refusal is
    worded the same wherever a person meets it.
    """
    found = pair(source, target)
    if found.support == "unsupported":
        return found.reason
    return ""


# --------------------------------------------------------------------------
# what a conversion costs, counted rather than described
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Loss:
    """What a particular conversation would lose on the way into a target.

    Counted from the conversation in hand, not recited from a table. A person
    deciding whether to convert wants to know that *this* conversation loses
    forty tool results, not that conversations in general might.
    """

    notes: tuple[str, ...] = ()
    degraded: int = 0
    """Blocks that survive as readable content but not as what they were."""

    @property
    def lossy(self) -> bool:
        return bool(self.notes)


def assess(conversation: Conversation, target: str) -> Loss:
    """What ``conversation`` would lose being written into ``target``.

    Returns an empty :class:`Loss` for a native import -- nothing is converted,
    so nothing is lost, and a dry run should say so rather than list
    reassurances.
    """
    if conversation.source_tool == target:
        return Loss()

    signed = 0
    calls = 0
    images = 0
    for message in conversation.messages:
        for block in message.content:
            if block.type == "thinking" and getattr(block, "signature", None):
                signed += 1
            elif block.type in ("tool_use", "tool_result"):
                calls += 1
            elif block.type == "image":
                images += 1

    notes: list[str] = []
    if signed:
        # Not "cannot be carried" -- cannot be *made*. The signature is issued
        # by the vendor whose model produced the thinking, and inventing one
        # would be forging an attestation. Dropped, and said out loud.
        notes.append(
            f"{signed} thinking blocks lose their signature: it is issued by the "
            f"model's vendor and cannot be reissued outside it"
        )
    if calls:
        notes.append(
            f"{calls} tool calls and results are kept as readable text, not as "
            f"tool calls {target} can run"
        )
    if images:
        notes.append(f"{images} image blocks are carried only if their bytes are in the bundle")
    notes.append(
        f"the conversation stays attributed to {conversation.source_tool} and to the "
        f"model that produced it; being written into {target} does not make it a "
        f"{target} conversation"
    )
    notes.append(CEILING)
    return Loss(notes=tuple(notes), degraded=signed + calls)
