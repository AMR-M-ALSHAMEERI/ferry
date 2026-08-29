"""Making a conversation the target tool's assistant can actually continue.

Migration has been asked to do two incompatible things. **Archive** keeps
everything -- thinking, tool calls, results, structure -- and serves reading,
searching and keeping a record. **Continue** serves picking the work up in the
target tool, and to do that it has to give up *more*, not less.

The tension is real and is not a shortcoming of the converter:

1. **A thinking block's signature cannot cross vendors.** It is issued by the
   model's own vendor and validated on every later turn. Ferry currently writes
   ``"signature": ""`` when it has none, which is the worst of the three
   options -- present and invalid. An absent thinking block is accepted; an
   invalid one is rejected outright, and the conversation cannot be continued
   at all.
2. **Tool calls name tools the target does not have.** A Codex conversation
   calls ``shell`` and ``apply_patch``; Copilot's calls ``copilot_readFile``;
   Antigravity's calls ``VIEW_FILE``. Written as tool calls into a tool with no
   such tools, they describe an assistant doing things that cannot have
   happened there.

So Continue mode drops the first and flattens the second into readable text.
What survives is what a person and a model both understand: **who said what.**

**This is a UCS -> UCS transform and runs before any writer.** One
implementation covers all four targets and every adapter's existing writer is
reused rather than forked -- the same win Compact got by reading UCS. It also
means the transform can be tested on its own, without a target tool anywhere
near it.

**Nothing here touches the bundle.** A new :class:`Conversation` is returned and
the original is left alone, so the backup stays lossless and the same bundle can
be imported again in Archive mode to get everything back.
"""

from __future__ import annotations

from dataclasses import dataclass

from ferry.compact.catalogue import describe
from ferry.compact.prune import error_line, failed
from ferry.ucs import Conversation, Message, TextBlock

__all__ = ["Flattened", "continuable", "prepare"]


@dataclass(frozen=True)
class Flattened:
    """What the transform gave up, counted rather than estimated."""

    thinking: int = 0
    calls: int = 0
    results: int = 0
    dropped: int = 0
    """Whole messages cut to fit the budget, oldest first."""

    @property
    def anything(self) -> bool:
        return bool(self.thinking or self.calls or self.results or self.dropped)


MAX_LINE = 120
"""How much of a flattened call or failure survives.

Measured twice, and both measurements moved it down. With **no** limit,
flattening the largest conversation here *grew* it from 210,819 words to
302,358: a ``Bash`` command can be an entire heredoc script, and 2,528 of them
outweighed every thinking block dropped. At Compact's 300, a Codex conversation
still grew -- 91,564 to 113,574 -- because 1,875 call lines at 300 characters
are worth more than 960 thinking blocks are worth removing.

**Compact's limit is the wrong one to borrow.** There a quotation is the
content; here a call line is *navigation* -- it exists so the next turn knows
roughly what was done, and nobody continues a conversation by re-reading a
command. 120 characters names the file or the command and stops.
"""

BUDGET = 30_000
"""Words kept, most recent first. ``0`` means all of them.

A conversation nobody can fit in a context window cannot be continued, whatever
else is true of it. Four of the nineteen conversations on this machine exceed
this after flattening; the largest is 153,398 words, which no assistant will
accept as a starting point.

**Kept from the end.** The recent turns are what someone is carrying on from,
and dropping the beginning is the loss they would have chosen. Whole messages
only -- half a turn is a turn the model has to guess at -- and what was dropped
is said in the transcript rather than silently missing.
"""


def _clip(text: str) -> str:
    """One line, shortened visibly if it would swamp the transcript."""
    line = " ".join(text.split())
    return line if len(line) <= MAX_LINE else line[: MAX_LINE - 3] + "..."


def _call_line(source_tool: str, name: str, arguments: object) -> str:
    """One tool call as a line a person and a model both read the same way.

    Built through :func:`ferry.compact.catalogue.describe`, which already knows
    what each tool's calls look like -- measured per tool from real stores. A
    second flattener written here would be a second place to keep correct, and
    the first one has the measurements behind it.
    """
    found = describe(source_tool, name, arguments if isinstance(arguments, dict) else None)
    if found.command:
        return _clip(f"[ran] {found.command}")
    if found.paths:
        verb = {"read": "read", "write": "edited", "search": "searched"}.get(found.kind, "used")
        return _clip(f"[{verb}] {', '.join(found.paths)}")
    # An unrecognised tool is named and nothing more. Guessing at what it did
    # would put a sentence in the transcript that nobody can source -- the same
    # rule Compact holds to, for the same reason.
    return f"[{name}]"


def continuable(
    conversation: Conversation, *, budget: int = BUDGET
) -> tuple[Conversation, Flattened]:
    """``conversation`` reduced to what a different assistant can carry on from.

    Args:
        conversation: Any UCS conversation. Not modified.
        budget: Words to keep, counted from the most recent message backwards.
            ``0`` keeps everything.

    Returns:
        A new conversation and a count of what was given up. Messages left with
        no content at all are dropped, because an empty turn in a transcript is
        a turn the model has to interpret and cannot.
    """
    thinking = calls = results = 0
    messages: list[Message] = []

    for message in conversation.messages:
        content: list[TextBlock] = []
        for block in message.content:
            if block.type == "text":
                if block.text.strip():
                    content.append(TextBlock(text=block.text))
            elif block.type == "thinking":
                # Dropped whole, never emitted unsigned. This is the block that
                # decides whether the conversation can be continued at all.
                thinking += 1
            elif block.type == "tool_use":
                calls += 1
                content.append(
                    TextBlock(text=_call_line(conversation.source_tool, block.name, block.input))
                )
            elif block.type == "tool_result":
                results += 1
                # The body is 79.3% of a conversation's bytes and is machinery.
                # A failure is the exception: it is why the next thing happened,
                # so the line that says so is kept and nothing else is.
                if failed(block.output):
                    line = error_line(_as_text(block.output))
                    if line:
                        content.append(TextBlock(text=_clip(f"[failed] {line}")))
            elif block.type == "image":
                # Kept as it is. An image is content someone chose to include,
                # and both a person and a model can still read it.
                content.append(block)  # type: ignore[arg-type]

        if content:
            messages.append(
                Message(
                    role=message.role,
                    content=content,  # type: ignore[arg-type]
                    timestamp=message.timestamp,
                    model=message.model,
                )
            )

    messages, dropped = _within(messages, budget)
    reduced = conversation.model_copy(update={"messages": messages}, deep=True)
    return reduced, Flattened(thinking=thinking, calls=calls, results=results, dropped=dropped)


def _within(messages: list[Message], budget: int) -> tuple[list[Message], int]:
    """The most recent ``budget`` words of ``messages``, and how many were cut.

    The first message kept is always taken whole however long it is: a budget
    that keeps nothing is worse than one that overruns, and the same rule holds
    in Compact for the same reason.
    """
    if budget <= 0 or not messages:
        return messages, 0

    kept: list[Message] = []
    spent = 0
    for message in reversed(messages):
        cost = sum(len(block.text.split()) for block in message.content if block.type == "text")
        if kept and spent + cost > budget:
            break
        spent += cost
        kept.append(message)
    kept.reverse()

    dropped = len(messages) - len(kept)
    if dropped:
        # Said in the transcript, not only in the counts. Whoever picks this up
        # -- person or model -- must be able to see that it starts partway
        # through, or they will read the first surviving turn as the beginning.
        kept.insert(
            0,
            Message(
                role="user",
                content=[
                    TextBlock(
                        text=(
                            f"[Ferry] {dropped} earlier messages are not carried into this "
                            f"copy. They are still in the bundle this was imported from."
                        )
                    )
                ],
            ),
        )
    return kept, dropped


def _as_text(output: object) -> str:
    """A tool result as text, however the adapter recorded it."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(_as_text(item) for item in output)
    if isinstance(output, dict):
        for key in ("text", "content", "output", "stdout"):
            if key in output:
                return _as_text(output[key])
    return str(output) if output is not None else ""


def prepare(
    conversation: Conversation, target: str, mode: str, *, budget: int = BUDGET
) -> tuple[Conversation, Flattened]:
    """The conversation an importer should actually write, and what it cost.

    The one place the decision is made, so every adapter answers it the same
    way. Returns the conversation untouched when there is nothing to decide:

    - **a restore** -- same tool at both ends -- is not a conversion and has
      nothing to trade, whatever mode was asked for;
    - **archive mode** is what Ferry has always done.
    """
    if conversation.source_tool == target or mode != "continue":
        return conversation, Flattened()
    return continuable(conversation, budget=budget)
