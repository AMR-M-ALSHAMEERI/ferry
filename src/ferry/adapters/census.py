"""Counting conversations the way the user counts them.

Every adapter used to report how many *files* it found and call that a
conversation count. On this machine that meant Copilot reporting 18 when VS
Code lists 5, and Antigravity reporting 6 when the app lists 2 -- and the gap
grows the more the tools are used, because the things being counted are not
conversations at all:

* **Empty ones.** VS Code writes a session file the moment a chat panel opens,
  whether or not anyone types. Twelve of eighteen files here are that.
* **Duplicates.** One conversation stored under two workspaces is one
  conversation.
* **Ones the app never shows.** An Antigravity subagent trajectory has its own
  database that looks exactly like a conversation.

A number the user can check against their own screen and finds wrong makes
every other number Ferry prints suspect, which is the real cost. So the rule
for all four adapters is now the same: **count what the application lists.**

The counting is cheap because it stops early. A conversation with messages is
recognised at its first one, and only an empty file is read to the end -- and
an empty file is small. Eighteen Copilot sessions take 60 milliseconds.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["Census", "census", "count_of", "jsonl_holds"]


@dataclass(frozen=True)
class Census:
    """What was on disk, and what of it is a conversation.

    Every field is a count of *files*, except :attr:`conversations`. They are
    kept apart rather than reduced to one number because the difference is the
    thing the user needs explained -- "18 files, 5 conversations" is an answer,
    "5" alone invites the question this class exists to settle.
    """

    files: int = 0
    conversations: int = 0
    empty: int = 0
    duplicates: int = 0
    hidden: int = 0
    """Files the application deliberately does not list, such as an Antigravity
    subagent trajectory. Carried by an export, never counted as conversations."""

    hidden_label: str = "not shown by the app"
    """How to describe the hidden ones. **Empty means say nothing here** -- an
    adapter with a better sentence of its own phrases it instead, and a note
    reading "1 " with the label missing is worse than no note at all."""

    def notes(self) -> list[str]:
        """Lines explaining any gap between the files and the count.

        Silent when there is no gap. A note saying "18 files, 18
        conversations" is noise, and the point of these is to answer a question
        the user would otherwise have to ask.
        """
        lines: list[str] = []
        if self.empty:
            lines.append(f"{self.empty} empty, never used")
        if self.duplicates:
            lines.append(
                f"{self.duplicates} duplicate {_plural(self.duplicates, 'copy', 'copies')} "
                "of a conversation stored more than once"
            )
        if self.hidden and self.hidden_label:
            lines.append(f"{self.hidden} {self.hidden_label}")
        return lines


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def count_of(number: int, singular: str, plural: str | None = None) -> str:
    """``"1 image"``, ``"22 images"``.

    A one-line function because the alternative that keeps appearing is
    "image(s)", and someone reading a count of their own conversations deserves
    a sentence rather than a placeholder standing in for one. It lives beside
    the census because every adapter that counts honestly then has to say the
    number out loud.
    """
    return f"{number} {_plural(number, singular, plural or singular + 's')}"


def census(
    items: Iterable[tuple[str, Path]],
    holds_messages: Callable[[str, Path], bool],
    *,
    hidden: Callable[[str, Path], bool] | None = None,
    hidden_label: str = "not shown by the app",
) -> Census:
    """Count conversations among ``(id, path)`` pairs.

    Args:
        items: The candidate files, each with the conversation id it claims.
            The id is what duplicates are detected by, so it must be the
            application's id and not the file's path.
        holds_messages: Whether a file holds an actual conversation.
        hidden: Whether the application deliberately does not list this one.
            Checked first, because a hidden file is neither a conversation nor
            an empty one and counting it as either would be wrong.
        hidden_label: How to describe those in a note.

    Returns:
        The counts. ``conversations`` is what the application should be
        showing.
    """
    files = empty = duplicates = concealed = 0
    seen: set[str] = set()

    for identifier, path in items:
        files += 1
        if hidden is not None and hidden(identifier, path):
            concealed += 1
            continue
        if not holds_messages(identifier, path):
            empty += 1
            continue
        if identifier in seen:
            duplicates += 1
            continue
        seen.add(identifier)

    return Census(
        files=files,
        conversations=len(seen),
        empty=empty,
        duplicates=duplicates,
        hidden=concealed,
        hidden_label=hidden_label,
    )


def jsonl_holds(path: Path, test: Callable[[dict[str, Any]], bool]) -> bool:
    """Whether any record in a JSONL file satisfies ``test``.

    **Stops at the first one.** That is what makes this affordable at detection
    time: a real transcript runs to megabytes, and the answer is almost always
    in its opening lines. Only a file with no messages at all is read to the
    end, and that file is small by definition.

    A line that will not parse is skipped rather than failing the whole file --
    a transcript being written while Ferry reads it can end mid-line, and that
    is not a reason to report the conversation as empty.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and test(record):
                    return True
    except OSError:
        # Unreadable is not empty. Reporting it as a conversation lets the
        # export be the thing that explains why it could not be read.
        return True
    return False
