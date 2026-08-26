"""Telling a person's words apart from what they pasted in.

The rule Compact is built on is that **the user's own words are kept verbatim**
-- they are 1.7% of a conversation's bytes, so there is nothing to gain by
compressing them and a great deal to lose. But a user message is not always the
user talking. Half of a long one is often a traceback, a diff or two hundred
lines of test output dropped in with "what is this?" at the top.

Those two are told apart **structurally, never by length.** Length is the
tempting signal and it is the wrong one: a long message is as likely to be a
dense instruction, where nearly every sentence is a decision, as it is to be a
log. Cutting by length throws away exactly the messages worth keeping.

So: fenced blocks come out, runs of machine-shaped lines come out, and whatever
prose is left is the person. When a message turns out to be *entirely* pasted,
it collapses to its error line and a note of how much was removed -- because
"the user pasted 214 lines ending in ImportError" is the fact, and the 214
lines are not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ferry.compact.prune import error_line

__all__ = ["Trimmed", "trim"]

_FENCE = re.compile(r"^\s*(?:```|~~~)")

_MACHINE = re.compile(
    r"^\s*(?:"
    r"Traceback \(most recent call last\)"  # the line that opens one
    r"|File \"[^\"]+\", line \d+"  # a python traceback frame
    r"|[A-Za-z_][\w.]*(?:Error|Exception):"  # the line that closes one
    r"|at [\w.$<>]+\([^)]*\)"  # a java or node frame
    r"|[-+]{3} [ab]?[/\\]?\S+"  # a diff header
    r"|@@ -\d+"  # a hunk marker
    r"|\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}"  # a log timestamp
    r"|\[\d{2}:\d{2}:\d{2}"  # a bracketed clock
    r"|\S+\.\w{1,5}:\d+(?::\d+)?[:\s]"  # path:line:col, every compiler
    r"|(?:PASS|FAIL|ok|FAILED|ERROR|WARN|INFO|DEBUG|TRACE)\b[\s:]"
    r"|\.{3,}\s*(?:ok|passed|failed)"
    r"|\s{4,}\S"  # a deeply indented continuation
    r")",
    re.IGNORECASE,
)
"""Line shapes that no one types into a chat box on purpose."""

_RUN = 3
"""How many machine-shaped lines in a row make a paste.

Three, because two is a coincidence -- a person writing about an error will
quote a line of it mid-sentence, and taking that away edits what they said.
"""

_WIDE = 180
"""A line this long with no sentence punctuation is output, not writing."""

_SENTENCE_END = re.compile(r"[.!?,;:]")


def _is_machine(line: str) -> bool:
    if _MACHINE.match(line):
        return True
    stripped = line.strip()
    if len(stripped) >= _WIDE and not _SENTENCE_END.search(stripped):
        return True
    return False


@dataclass(frozen=True)
class Trimmed:
    """What was left of a user message, and what was taken out of it."""

    text: str
    """The person's own words, verbatim. Never rewritten, only cut."""

    removed: int = 0
    """Lines of pasted material removed."""

    error: str = ""
    """The first error line inside what was removed, when there was one."""

    segments: tuple[str, ...] = ()
    """The surviving prose as **contiguous runs**, in order.

    ``text`` is these joined together, which is right for quoting whole and
    wrong for quoting a piece. Once a paste is cut out of the middle of a
    message, two lines that are now neighbours were not neighbours in the
    original -- so a sentence taken across that seam is a sentence the person
    never wrote, assembled from two they did.

    Found by the self-check on real data: a 297-character opening quotation
    could not be located in the conversation it came from, because it spanned
    the hole where a traceback had been. Anything that takes a *fragment* takes
    it from one segment.
    """

    @property
    def is_paste(self) -> bool:
        """True when the message was pasted material and nothing else."""
        return self.removed > 0 and not self.text.strip()


def trim(text: str) -> Trimmed:
    """Separate a user message into what they wrote and what they pasted.

    Args:
        text: One user ``TextBlock``.

    Returns:
        A :class:`Trimmed`. When nothing was pasted, ``text`` is the input
        unchanged -- byte for byte, including its blank lines.
    """
    lines = text.splitlines()
    kept: list[str] = []
    removed: list[str] = []
    run: list[str] = []
    fenced = False
    segments: list[list[str]] = []
    broken = False

    def flush(*, machine: bool) -> None:
        """Move the pending run into whichever side it belongs to."""
        nonlocal broken
        if not run:
            return
        if machine:
            removed.extend(run)
            broken = True
        else:
            kept.extend(run)
            _extend(run)
        run.clear()

    def _extend(more: list[str]) -> None:
        """Add lines to the current contiguous run, starting a new one if the
        last thing to happen was a removal."""
        nonlocal broken
        if broken or not segments:
            segments.append([])
            broken = False
        segments[-1].extend(more)

    for line in lines:
        if _FENCE.match(line):
            # A fence is a boundary in both directions: what came before it is
            # settled, and the fence itself belongs to the block it opens.
            flush(machine=len(run) >= _RUN)
            fenced = not fenced
            removed.append(line)
            broken = True
            continue
        if fenced:
            removed.append(line)
            broken = True
            continue
        if _is_machine(line):
            run.append(line)
            continue
        # Prose. Anything pending was a short run inside writing, so it stays.
        flush(machine=len(run) >= _RUN)
        kept.append(line)
        _extend([line])
    flush(machine=len(run) >= _RUN)

    if not removed:
        return Trimmed(text=text, segments=(text,))

    body = "\n".join(kept).strip("\n")
    # Collapse the blank lines the removal left behind, so a message does not
    # arrive full of holes where its log used to be.
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    runs = tuple(cleaned for cleaned in ("\n".join(run).strip() for run in segments) if cleaned)
    return Trimmed(
        text=body,
        removed=len(removed),
        error=error_line("\n".join(removed)),
        segments=runs,
    )
