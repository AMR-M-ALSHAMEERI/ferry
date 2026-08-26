"""Stage 2: throwing away the ninety-one percent.

``tool_result`` is 79.3% of a conversation's bytes and ``tool_use`` another
11.5%. Nearly all of it is machinery -- the contents of a file that was read,
the whole of a file that was written, ten thousand lines of test output. The
compaction target is met here, before any ranking runs, by keeping four facts
about each call and discarding its body:

- which tool it was, and what it did (:mod:`ferry.compact.catalogue`),
- what it did it to,
- whether it worked,
- and if it did not, the one line that says why.

**Whether it worked is answered structurally where the tool says so**, and only
otherwise by pattern. That distinction matters more than it looks: measured
over 6,442 real results, the word "failed" appears in 15% of Codex output and
9.8% of Claude Code's -- overwhelmingly because a test run *reported* failures,
which is the tool succeeding at its job. Treating that as a failed call would
misreport hundreds of calls per conversation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ferry.compact.catalogue import OTHER, Call, describe
from ferry.ucs import Conversation

__all__ = ["Fact", "error_line", "facts", "failed"]


_NAMED_ERROR = re.compile(
    r"^[ \t]*(?:"
    r"[A-Za-z_][\w.]*(?:Error|Exception)\b.*"
    r"|error(?:[ \t]*\[[^\]]+\])?[ \t]*:.*"
    r"|fatal:.*"
    r")$",
    re.IGNORECASE | re.MULTILINE,
)
"""Lines that **name** what went wrong.

Every alternative was measured against the human's own stores
(``spikes/probe_errors.py``, counts only). What is *not* here is as deliberate:
a bare "failed" or "failure" fires on 15% of Codex results and almost all of
them are a passing tool reporting a failing test.

Anchored at the front of a line and nowhere else. An earlier version wrote the
phrase cases as ``.*phrase.*$``, which asks the engine to try every starting
position on every line, and cost three seconds on one conversation. Anchored
alternatives and a plain substring search do the same work in milliseconds --
see :data:`_ERROR_PHRASE`.
"""

_OPENING = re.compile(r"^[ \t]*Traceback \(most recent call last\).*$", re.MULTILINE)
"""A traceback announcing itself. Matched, but never quoted in preference to
the exception at the bottom -- "Traceback (most recent call last):" tells a
reader only that there was one."""

_ERROR_PHRASE = re.compile(
    r"command not found"
    r"|is not recognized as an internal"
    r"|No such file or directory"
    r"|cannot find the path"
    r"|The system cannot find"
    r"|Permission denied"
    r"|Access is denied",
    re.IGNORECASE,
)
"""Errors that announce themselves in the middle of a line rather than at the
front of one. Found by substring search, then widened to the line around it."""

_MAX_ERROR_LINE = 200

_WINDOW = 8192
"""How much of a tool result is searched for an error line, at each end.

A single ``tool_result`` on this machine reaches two megabytes, and scanning
every byte of every one of them cost seven seconds on the largest conversation
-- against a budget of two. Errors announce themselves at the top of the output
or at the bottom of it; the middle is the file, the diff or the test log.

**The cost of the window is real and worth naming**: an error line buried in the
middle of a megabyte of output is not found. It is a deliberate trade of a rare
miss for a feature that finishes while someone is still looking at the screen.
"""


def _ends(text: str) -> str:
    """The head and tail of a long string, which is where errors live."""
    if len(text) <= _WINDOW * 2:
        return text
    return "\n".join((text[:_WINDOW], text[-_WINDOW:]))


def _as_text(output: Any) -> str:
    """Whatever text a tool result carries, in whatever shape it arrived.

    Three shapes occur in real bundles and all three are ordinary: a plain
    string (Claude Code, always), a list of parts (Codex, 2,095 times), and a
    dict (Codex's ``Ok``/``Err`` envelope, Copilot's terminal result).
    """
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "content", "output", "value"):
                    if isinstance(item.get(key), str):
                        parts.append(item[key])
                        break
        return "\n".join(parts)
    if isinstance(output, dict):
        for key in ("output", "text", "content", "stdout", "Ok", "Err"):
            value = output.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, list | dict):
                return _as_text(value)
    return ""


def error_line(text: str) -> str:
    """The first line of ``text`` that announces an error, or ``""``.

    Returned verbatim, trimmed only of surrounding whitespace and of length --
    a single traceback line can be a thousand characters of embedded JSON, and
    a document that quotes it whole is unreadable.
    """
    windowed = _ends(text)
    found = _NAMED_ERROR.search(windowed)
    if found is not None:
        return _cut(found.group(0))

    phrase = _ERROR_PHRASE.search(windowed)
    if phrase is not None:
        start = windowed.rfind("\n", 0, phrase.start()) + 1
        end = windowed.find("\n", phrase.end())
        return _cut(windowed[start:] if end < 0 else windowed[start:end])

    opening = _OPENING.search(windowed)
    return _cut(opening.group(0)) if opening else ""


def _cut(line: str) -> str:
    """One line, collapsed and shortened. A traceback line can be a thousand
    characters of embedded JSON, and a document that quotes it whole is
    unreadable."""
    collapsed = " ".join(line.split())
    if len(collapsed) <= _MAX_ERROR_LINE:
        return collapsed
    return collapsed[: _MAX_ERROR_LINE - 3] + "..."


def failed(output: Any) -> bool | None:
    """Did this call fail?

    Returns:
        ``True`` or ``False`` when the tool said so or the output announces an
        error; ``None`` when there is nothing to go on. **None is not False.**
        A conversation where no tool ever reported an outcome should say
        nothing about outcomes rather than claim everything succeeded.
    """
    if isinstance(output, dict):
        if isinstance(output.get("isError"), bool):
            return bool(output["isError"])
        if "Err" in output:
            return True
        if "Ok" in output:
            return False
    text = _as_text(output)
    if not text.strip():
        return None
    windowed = _ends(text)
    return bool(
        _NAMED_ERROR.search(windowed) or _ERROR_PHRASE.search(windowed) or _OPENING.search(windowed)
    )


@dataclass(frozen=True)
class Fact:
    """One tool call, with its body gone and its meaning kept."""

    name: str
    call: Call
    ok: bool | None = None
    error: str = ""
    answered_at: int = -1
    """Index of the message that carried this call's result, or -1.

    Here so that :mod:`ferry.compact.extract` can find the prose either side of
    a failure **without judging every tool result a second time.** Doing it
    twice cost more than the whole of the rest of the pipeline: the error
    patterns run over megabytes of output, and running them once is the
    difference between one second and seven.
    """


def facts(conversation: Conversation) -> list[Fact]:
    """Every tool call in a conversation, pruned to facts, in order.

    Calls and results are paired by ``tool_use_id``. Where a result names no
    call -- 255 of 6,442 on the probe machine, all Codex ``Ok`` envelopes -- its
    error line still counts, under an empty tool name. Dropping it would hide
    real failures for the sake of a tidy table.
    """
    tool = conversation.source_tool
    calls: dict[str, tuple[int, str, Call]] = {}
    order: list[Fact] = []
    outcomes: dict[str, tuple[bool | None, str, int]] = {}
    orphans: list[Fact] = []

    for position, message in enumerate(conversation.messages):
        for block in message.content:
            if block.type == "tool_use":
                call = describe(tool, block.name, block.input)
                order.append(Fact(name=block.name, call=call))
                if block.id:
                    calls[block.id] = (len(order) - 1, block.name, call)
            elif block.type == "tool_result":
                verdict = failed(block.output)
                line = error_line(_as_text(block.output)) if verdict else ""
                if block.tool_use_id in calls:
                    outcomes[block.tool_use_id] = (verdict, line, position)
                elif line:
                    orphans.append(
                        Fact(name="", call=OTHER, ok=False, error=line, answered_at=position)
                    )

    for call_id, (index, name, call) in calls.items():
        verdict, line, position = outcomes.get(call_id, (None, "", -1))
        ok = None if verdict is None else not verdict
        order[index] = Fact(name=name, call=call, ok=ok, error=line, answered_at=position)

    return order + orphans
