"""Stage 4: writing the document.

The output contract, which is the whole point of the feature:

1. **Every line is quoted verbatim or is a count.** Nothing here composes a
   sentence about the conversation. Ferry's own words appear only as headings
   and as the fixed footer, and :func:`quotations` exists so a test can pull
   out everything that claims to be a quotation and check it really is one.
2. **A section with nothing in it is omitted**, never padded. "Files touched:
   none" for a session that touched forty would be a lie told by omission, so
   where a tool cannot yield a ledger the section says *why* instead.
3. **The footer states what the document is and is not**, including how
   complete the source tool's record was.

Quotations are marked so both a reader and a test can find them: prose in
blockquotes, and every path, command and error line inside backticks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from ferry.compact.extract import Digest
from ferry.compact.rank import top

__all__ = ["LENGTHS", "SHAPES", "Limits", "quotations", "render"]

SHAPES = ("handoff", "said", "done")
"""``handoff`` is everything; ``said`` is only the user's words; ``done`` is
only what the tools did. The names are what the menu shows, so they are the
person's words for it rather than Ferry's."""


@dataclass(frozen=True)
class Limits:
    """How much of each section a given length shows. ``0`` means all of it.

    ``said`` and ``closing`` are budgets in **words**; the rest are counts of
    rows. Words, because the thing being limited is how long the document is to
    read, and one person's twenty messages are another's two hundred.
    """

    said: int = 0
    closing: int = 0
    files: int = 0
    commands: int = 0
    errors: int = 0
    prose: int = 0
    threads: int = 0


LENGTHS: dict[str, Limits] = {
    "brief": Limits(said=250, closing=120, files=8, commands=6, errors=4, prose=4, threads=4),
    "standard": Limits(said=900, closing=300, files=20, commands=15, errors=8, prose=12, threads=8),
    "full": Limits(said=0, closing=0, files=60, commands=40, errors=20, prose=40, threads=20),
}
"""Roughly 400, 1,200 and 3,500 words.

**Full keeps every message the person typed.** Brief and Standard keep the most
recent ones and say how many they left out.

That cap was not in the first draft, and the measurement is why it is here.
The user's words are 1.7% of a conversation's bytes, which sounds like a
rounding error until the conversation is nineteen megabytes -- and then it is
three hundred kilobytes of Standard, a document nobody reads. Capping is not
the same as rewriting: the messages that are shown are still word for word,
and the ones that are not are counted rather than quietly dropped.
"""

_TOOL_NAMES = {
    "claude-code": "Claude Code",
    "codex": "OpenAI Codex",
    "copilot": "GitHub Copilot Chat",
    "antigravity": "Antigravity",
}


def _when(started: datetime, ended: datetime) -> str:
    first = started.strftime("%d %b %Y")
    last = ended.strftime("%d %b %Y")
    return first if first == last else f"{started.strftime('%d %b')} to {last}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _quote(text: str) -> list[str]:
    """A blockquote, verbatim, with the quote marker on every line."""
    return [f"> {line}" if line.strip() else ">" for line in text.splitlines()]


def _limit(rows: tuple, count: int) -> tuple:  # type: ignore[type-arg]
    return rows if count <= 0 else rows[:count]


# --------------------------------------------------------------------------
# the sections
# --------------------------------------------------------------------------


def _header(digest: Digest, version: str) -> list[str]:
    title = digest.title.strip() or "an untitled conversation"
    where = digest.workspace.strip()
    line = " - ".join(
        part for part in (_TOOL_NAMES.get(digest.source_tool, digest.source_tool), where) if part
    )
    # "0 files touched" for a session that edited twenty would be a false
    # summary in the first line of the document. Where a tool's file record is
    # incomplete, the count says what it actually counted.
    verb = " read" if digest.fidelity.files else " touched"
    counts = [
        _plural(digest.messages, "message"),
        _plural(len(digest.files), "file") + verb,
        _plural(digest.tool_calls, "tool call"),
    ]
    if digest.images:
        counts.append(_plural(digest.images, "image"))
    return [
        f"# Compact: {title}",
        "",
        f"{line} - {_when(digest.started, digest.ended)}",
        f"{' - '.join(counts)} - built by Ferry {version}, nothing sent anywhere",
    ]


def _said(digest: Digest, limits: Limits) -> list[str]:
    if not digest.said:
        return []
    shown = digest.said
    dropped = 0
    if limits.said > 0:
        budget = limits.said
        kept = 0
        taken = 0
        # Backwards from the end: the most recent messages are the ones a
        # handoff is for. Whole messages only -- a message is either quoted as
        # the person wrote it, or counted as one that was not shown. There is
        # no third option where Ferry decides which half of a sentence
        # mattered. The first message is always taken, however long it is,
        # because a budget that shows nothing is worse than one that overruns.
        for item in reversed(shown):
            cost = len(item.text.split()) or 1
            if kept and taken + cost > budget:
                break
            taken += cost
            kept += 1
        dropped = len(shown) - kept
        shown = shown[len(shown) - kept :]

    out = ["## What you said", ""]
    if dropped:
        out += [f"*{_plural(dropped, 'earlier message')} not shown at this length.*", ""]
    for item in shown:
        if item.text.strip():
            out += _quote(item.text)
            if item.removed:
                out.append(f"> *({_plural(item.removed, 'pasted line')} removed)*")
        elif item.error:
            out.append(f"> *({_plural(item.removed, 'pasted line')}, ending in)* `{item.error}`")
        else:
            out.append(f"> *({_plural(item.removed, 'pasted line')} removed)*")
        out.append("")
    return out


def _files(digest: Digest, limits: Limits) -> list[str]:
    if digest.fidelity.files and not digest.files:
        return ["## Files touched", "", f"*{digest.fidelity.files}*", ""]
    if not digest.files:
        return []
    out = ["## Files touched", ""]
    for entry in _limit(digest.files, limits.files):
        parts = []
        if entry.edited:
            parts.append(f"edited {entry.edited}")
        if entry.read:
            parts.append(f"read {entry.read}")
        out.append(f"- `{entry.path}` - {', '.join(parts)}")
    hidden = len(digest.files) - len(_limit(digest.files, limits.files))
    if hidden:
        out.append(f"- *and {_plural(hidden, 'other file')}*")
    if digest.fidelity.files:
        out += ["", f"*{digest.fidelity.files}*"]
    return [*out, ""]


def _commands(digest: Digest, limits: Limits) -> list[str]:
    if digest.fidelity.commands and not digest.commands:
        return ["## Commands run", "", f"*{digest.fidelity.commands}*", ""]
    if not digest.commands:
        return []
    out = ["## Commands run", ""]
    for entry in _limit(digest.commands, limits.commands):
        detail = f"{_plural(entry.times, 'time')}"
        if entry.failed:
            detail += f", {entry.failed} failed"
        out.append(f"- `{entry.text}` - {detail}")
    hidden = len(digest.commands) - len(_limit(digest.commands, limits.commands))
    if hidden:
        out.append(f"- *and {_plural(hidden, 'other command')}*")
    return [*out, ""]


def _errors(digest: Digest, limits: Limits) -> list[str]:
    if not digest.errors:
        return []
    out = ["## Errors seen", ""]
    for entry in _limit(digest.errors, limits.errors):
        out.append(f"- `{entry.text}` - {_plural(entry.times, 'time')}")
    hidden = len(digest.errors) - len(_limit(digest.errors, limits.errors))
    if hidden:
        out.append(f"- *and {_plural(hidden, 'other error')}*")
    return [*out, ""]


def _prose(digest: Digest, limits: Limits) -> list[str]:
    chosen = top(digest.prose, limit=limits.prose)
    if not chosen:
        return []
    out = ["## What was worked out along the way", ""]
    for line in chosen:
        out += [f"> {line}", ""]
    return out


def _words(text: str, budget: int) -> str:
    """The opening of a message, cut at a word boundary and marked as cut."""
    if budget <= 0:
        return text
    words = text.split()
    if len(words) <= budget:
        return text
    return " ".join(words[:budget]) + " ..."


def _ending(digest: Digest, limits: Limits) -> list[str]:
    if not digest.closing:
        return []
    out = ["## Where it ended", ""]
    for text in digest.closing:
        out += _quote(_words(text, limits.closing))
        out.append("")
    return out


def _threads(digest: Digest, limits: Limits) -> list[str]:
    if not digest.open_threads:
        return []
    out = ["## Still open", ""]
    for line in _limit(digest.open_threads, limits.threads):
        out.append(f"- {line}")
    return [*out, ""]


def _footer(digest: Digest) -> list[str]:
    tier = {
        "complete": "Its record of files and commands is complete.",
        "good": "Its record of files and commands is good.",
        "partial": "Its record is partial, and the gaps are named above.",
        "unknown": "Ferry has no catalogue for this tool.",
    }[digest.fidelity.tier]
    return [
        "---",
        "",
        f"Built from {_plural(digest.messages, 'message')} without sending them anywhere.",
        "Every line above is quoted from the conversation or counted from it.",
        tier,
    ]


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------


def render(
    digest: Digest, *, shape: str = "handoff", length: str = "standard", version: str = ""
) -> str:
    """The document.

    Args:
        digest: What :func:`ferry.compact.extract.digest` found.
        shape: One of :data:`SHAPES`.
        length: One of :data:`LENGTHS`.
        version: Ferry's version, for the header. Passed in rather than looked
            up so this module stays free of imports that touch the outside.

    Returns:
        Markdown, ending in a single newline.
    """
    limits = LENGTHS.get(length, LENGTHS["standard"])
    lines = [*_header(digest, version or "0.1.0"), ""]

    if digest.asked and shape != "done":
        lines += ["## What you asked for", "", *_quote(digest.asked), ""]

    if shape in ("handoff", "said"):
        lines += _said(digest, limits)
    if shape in ("handoff", "done"):
        lines += _files(digest, limits)
        lines += _commands(digest, limits)
        lines += _errors(digest, limits)
    if shape == "handoff":
        lines += _prose(digest, limits)
    if shape in ("handoff", "done"):
        lines += _ending(digest, limits)
    if shape in ("handoff", "said"):
        lines += _threads(digest, limits)

    lines += _footer(digest)
    return "\n".join(lines).rstrip("\n") + "\n"


# --------------------------------------------------------------------------
# the property the whole feature rests on
# --------------------------------------------------------------------------

_BACKTICKED = re.compile(r"`([^`]+)`")


def quotations(document: str) -> list[str]:
    """Everything in ``document`` that claims to be taken from the source.

    Blockquote bodies and anything inside backticks. A test feeds these back
    against the conversation they came from, and that test is what makes
    "nothing was invented" a fact rather than an intention.

    Ferry's own asides are wrapped in asterisks and skipped here -- they are
    counts about the document, not claims about the conversation.
    """
    found: list[str] = []
    for line in document.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            body = stripped[1:].strip()
            if body and not body.startswith("*"):
                found.append(body)
        elif stripped.startswith("- "):
            body = stripped[2:].strip()
            if body and not body.startswith(("*", "`")):
                found.append(body)
        found.extend(_BACKTICKED.findall(stripped))
    return [item for item in found if item]
