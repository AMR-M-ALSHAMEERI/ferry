"""Stage 3: the ledgers.

Once the bodies are gone (:mod:`ferry.compact.prune`) what is left is countable.
This module does the counting and nothing else -- it produces a :class:`Digest`
of facts and quotations, and has no opinion about how any of it is laid out.
Keeping the two apart is what lets the whole pipeline be tested without a
screen: a digest is data, and the markdown is one rendering of it.

Every field here is either **something the conversation says, verbatim**, or a
number derived from it. There is no field whose value Ferry made up, and that
is the property the output contract rests on.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from ferry.compact.catalogue import Fidelity, fidelity
from ferry.compact.paste import Trimmed, trim
from ferry.compact.prune import Fact, facts
from ferry.ucs import Conversation

__all__ = ["Digest", "FileEntry", "Ledger", "Said", "digest"]


@dataclass(frozen=True)
class FileEntry:
    """One path, and what was done to it."""

    path: str
    edited: int = 0
    read: int = 0

    @property
    def weight(self) -> tuple[int, int]:
        """Edits first, then reads. A file that was written to matters more
        than one that was glanced at, however many times it was glanced at."""
        return (self.edited, self.read)


@dataclass(frozen=True)
class Ledger:
    """One line that appeared more than once: a command, or an error."""

    text: str
    times: int = 1
    failed: int = 0


@dataclass(frozen=True)
class Said:
    """One thing the user said, with anything they pasted taken out of it."""

    text: str
    removed: int = 0
    error: str = ""
    segments: tuple[str, ...] = ()
    """The surviving prose as contiguous runs. See :class:`~ferry.compact.paste.Trimmed`."""


@dataclass(frozen=True)
class Digest:
    """Everything Compact knows about a conversation, before it is written out."""

    title: str
    source_tool: str
    workspace: str
    started: datetime
    ended: datetime
    fidelity: Fidelity

    messages: int = 0
    tool_calls: int = 0
    images: int = 0

    files: tuple[FileEntry, ...] = ()
    commands: tuple[Ledger, ...] = ()
    errors: tuple[Ledger, ...] = ()

    asked: str = ""
    """The first thing the user said. Verbatim."""

    said: tuple[Said, ...] = ()
    closing: tuple[str, ...] = ()
    open_threads: tuple[str, ...] = ()

    anchors: tuple[str, ...] = field(default_factory=tuple)
    """Assistant prose kept unconditionally: the last two messages, and any
    message that sat next to a failure."""

    prose: tuple[str, ...] = field(default_factory=tuple)
    """The rest of the assistant's prose, for :mod:`ferry.compact.rank` to
    choose from. Never rendered whole."""


# --------------------------------------------------------------------------
# open threads
# --------------------------------------------------------------------------

_LEAVING_OPEN = re.compile(
    r"\b(?:TODO|FIXME|later|next time|for now|not yet|still (?:need|have|to)|"
    r"come back to|leave (?:it|that)|another session|tomorrow|revisit)\b",
    re.IGNORECASE,
)
"""Language that says a thing was put down rather than finished.

Matched against the **user's** sentences only. The assistant says "for now" as
a turn of phrase constantly; when the person says it, they are describing the
state of their work.
"""

_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")

_MAX_QUOTE = 300


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE.split(text) if part.strip()]


def _clip(text: str) -> str:
    """A quotation, shortened only if it would swamp the page.

    Shortening is visible -- the ellipsis is there so a reader can tell a
    trimmed quote from a whole one, and so the quotation test knows to compare
    only the part that was kept.
    """
    line = " ".join(text.split())
    return line if len(line) <= _MAX_QUOTE else line[: _MAX_QUOTE - 3] + "..."


# --------------------------------------------------------------------------
# the walk
# --------------------------------------------------------------------------


def _ledger(counts: Counter[str], failures: Counter[str]) -> tuple[Ledger, ...]:
    """Counted lines, most frequent first, then alphabetically.

    The second key is not decoration. Without it two commands run the same
    number of times could swap places between runs, and the determinism test --
    which is the one that proves this feature has no moving parts -- would fail
    intermittently, which is worse than failing.
    """
    rows = [Ledger(text=text, times=n, failed=failures.get(text, 0)) for text, n in counts.items()]
    rows.sort(key=lambda row: (-row.times, row.text))
    return tuple(rows)


def _user_blocks(conversation: Conversation) -> list[Trimmed]:
    found: list[Trimmed] = []
    for message in conversation.messages:
        if message.role != "user":
            continue
        for block in message.content:
            if block.type == "text" and block.text.strip():
                found.append(trim(block.text))
    return found


def _tool_ledgers(
    conversation: Conversation,
) -> tuple[tuple[FileEntry, ...], tuple[Ledger, ...], tuple[Ledger, ...], int, frozenset[int]]:
    """The three ledgers, the call count, and which messages held a failure.

    All of it from one pass. Judging a tool result means running the error
    patterns over its whole output, and a single result here reaches two
    megabytes -- so the walk that builds the ledgers also reports where the
    failures were, rather than letting the prose anchors go and look again.
    Doing it twice cost more than the whole of the rest of the pipeline.
    """
    edits: Counter[str] = Counter()
    reads: Counter[str] = Counter()
    commands: Counter[str] = Counter()
    command_failures: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    failures: set[int] = set()

    found: list[Fact] = facts(conversation)
    for fact in found:
        if fact.call.kind == "write":
            for path in fact.call.paths:
                edits[path] += 1
        elif fact.call.kind == "read":
            for path in fact.call.paths:
                reads[path] += 1
        if fact.call.kind == "command" and fact.call.command:
            line = _clip(fact.call.command)
            commands[line] += 1
            if fact.ok is False:
                command_failures[line] += 1
        if fact.error:
            errors[fact.error] += 1
        if fact.ok is False and fact.answered_at >= 0:
            failures.add(fact.answered_at)

    paths = sorted(set(edits) | set(reads))
    files = tuple(
        sorted(
            (
                FileEntry(path=path, edited=edits.get(path, 0), read=reads.get(path, 0))
                for path in paths
            ),
            key=lambda entry: (-entry.weight[0], -entry.weight[1], entry.path),
        )
    )
    return (
        files,
        _ledger(commands, command_failures),
        _ledger(errors, Counter()),
        sum(1 for fact in found if fact.name),
        frozenset(failures),
    )


def _prose(
    conversation: Conversation, failures: frozenset[int]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Assistant prose, split into what is kept outright and what is ranked.

    The last two messages are kept because "where it ended" is the single most
    useful thing in a handoff. A message that sits beside a failure is kept
    because that is where the assistant said what went wrong -- and a ranker
    scoring sentences by term frequency has no way to know that.
    """
    speeches: list[str] = []
    keep: set[int] = set()
    after_failure = False

    for position, message in enumerate(conversation.messages):
        # One block at a time, never joined. A message's text blocks are not
        # necessarily neighbours: Copilot puts a tool call between two of them
        # and turns a named file into a block of its own. Joining them makes a
        # passage the assistant never said in one breath -- two things it did
        # say, glued across whatever happened in between. The self-check found
        # this as an 811-character quotation that was nowhere in the source.
        for block in message.content:
            if block.type != "text" or not block.text.strip():
                continue
            if message.role != "assistant":
                continue
            speeches.append(block.text)
            if after_failure:
                keep.add(len(speeches) - 1)
                after_failure = False

        if position in failures:
            # Both sides of it: what the assistant had just said, and what it
            # says next.
            after_failure = True
            if speeches:
                keep.add(len(speeches) - 1)

    keep.update(index for index in (len(speeches) - 1, len(speeches) - 2) if index >= 0)
    anchors = tuple(speeches[index] for index in sorted(keep))
    pool = tuple(text for index, text in enumerate(speeches) if index not in keep)
    return anchors, pool


def digest(conversation: Conversation) -> Digest:
    """Everything Compact knows about one conversation.

    Touches no files, no network and no clock -- which is what makes the whole
    of this pipeline testable without a terminal.
    """
    spoken = _user_blocks(conversation)
    files, commands, errors, calls, failures = _tool_ledgers(conversation)
    anchors, pool = _prose(conversation, failures)

    said = tuple(
        Said(
            text=item.text,
            removed=item.removed,
            error=item.error,
            segments=item.segments,
        )
        for item in spoken
        if item.text.strip() or item.error or item.removed
    )

    # Sentence by sentence, **within a segment**. Once a paste has been cut out
    # of a message, a sentence taken across the gap is one the person never
    # wrote -- two of theirs joined at the seam. Found by the self-check on real
    # data rather than by reasoning about it.
    open_threads: list[str] = []
    for item in spoken:
        for segment in item.segments:
            for sentence in _sentences(segment):
                if _LEAVING_OPEN.search(sentence):
                    open_threads.append(_clip(sentence))

    workspace = conversation.workspace.original_path or conversation.workspace.name or ""

    return Digest(
        title=conversation.title or "",
        source_tool=conversation.source_tool,
        workspace=workspace,
        started=conversation.created_at,
        ended=conversation.updated_at,
        fidelity=fidelity(conversation.source_tool),
        messages=len(conversation.messages),
        tool_calls=calls,
        images=sum(
            1 for m in conversation.messages for block in m.content if block.type == "image"
        ),
        files=files,
        commands=commands,
        errors=errors,
        asked=_clip(said[0].segments[0]) if said and said[0].segments else "",
        said=said,
        closing=anchors[-2:],
        open_threads=tuple(dict.fromkeys(open_threads)),
        anchors=anchors,
        prose=pool,
    )
