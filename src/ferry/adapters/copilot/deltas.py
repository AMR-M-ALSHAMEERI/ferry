"""Replaying a Copilot Chat transcript.

**This is the highest-risk component of the Copilot adapter, and the reason it
is built first.**

A session file is not a document. It is a log of edits to one: the first line
is a snapshot, every line after it changes the snapshot in place. Reconstructing
the conversation means applying all of them in order.

Getting this wrong does not produce an error. On the probe machine **all four
session files had ``requests: []`` in their snapshot** and every real turn
arrived later as an append. A reader that took the snapshot and stopped would
return a perfectly well-formed conversation object containing no messages at
all, and would report success while doing it. That is the failure this module
exists to prevent, and it is why the replayer is tested on its own rather than
only through the adapter.

Record shape, verified against VS Code 1.134.0::

    {"kind": 0, "v": {...}}                     snapshot, replaces everything
    {"kind": 1, "k": ["a", "b"], "v": x}        set: doc["a"]["b"] = x
    {"kind": 2, "k": ["requests"], "v": [y]}    append: doc["requests"] += [y]
    {"kind": 2, "k": [...], "i": 5, "v": [y]}   splice: cut to 5, then append

**The ``i`` field is the trap in this format.** It was absent from the first
sample entirely and appeared only once there was real tool use to look at.
It is not a position for the new items -- it is where the existing list is
**cut** before they are added, because VS Code revises a response as it
streams and re-sends the tail. Ignoring it does not lose data, it invents
data: see :func:`_spliced`.

Only kinds 0, 1 and 2 have been observed. **The set is assumed incomplete** --
the first sample had 18 records and missed ``i`` outright -- so an unrecognised
kind is counted and skipped, never raised. A future VS Code that adds kind 3
should cost the user the records it could not read, not the whole conversation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["SNAPSHOT", "SET", "APPEND", "ReplayResult", "replay", "replay_lines"]

SNAPSHOT = 0
"""``kind`` 0 -- ``v`` is the whole document."""

SET = 1
"""``kind`` 1 -- assign ``v`` at path ``k``."""

APPEND = 2
"""``kind`` 2 -- extend the list at path ``k`` with ``v``."""


@dataclass
class ReplayResult:
    """A rebuilt document, and an honest account of what did not apply.

    The counts exist so the adapter can warn. A conversation rebuilt from a
    file with skipped records is not necessarily wrong, but the user is
    entitled to know it happened rather than to be handed a confident-looking
    result.
    """

    document: dict[str, Any] = field(default_factory=dict)
    applied: int = 0
    unparseable: int = 0
    unknown_kinds: dict[int, int] = field(default_factory=dict)
    bad_paths: int = 0

    @property
    def skipped(self) -> int:
        """Records that did not make it into the document."""
        return self.unparseable + self.bad_paths + sum(self.unknown_kinds.values())

    @property
    def clean(self) -> bool:
        """Whether every record in the file was understood and applied."""
        return self.skipped == 0


def _descend(doc: dict[str, Any], path: list[Any]) -> dict[str, Any] | list[Any] | None:
    """Walk to the container holding the last path segment, creating dicts.

    Returns ``None`` when the path runs through something that is not a
    container -- a set into ``doc["a"]["b"]`` where ``doc["a"]`` is a string
    cannot be honoured, and inventing a dict there would silently discard the
    string that was really at that position.
    """
    node: Any = doc
    for step in path[:-1]:
        if isinstance(node, list):
            if not isinstance(step, int) or not -len(node) <= step < len(node):
                return None
            node = node[step]
            continue
        if not isinstance(node, dict):
            return None
        if step not in node:
            node[step] = {}
        elif not isinstance(node[step], dict | list):
            # Something real is already here. Replacing it with a container to
            # make the path fit would discard it without a word.
            return None
        node = node[step]
    return node if isinstance(node, dict | list) else None


def _assign(container: Any, key: Any, value: Any) -> bool:
    if isinstance(container, list):
        if not isinstance(key, int) or not -len(container) <= key < len(container):
            return False
        container[key] = value
        return True
    if isinstance(container, dict):
        container[key] = value
        return True
    return False


def _spliced(existing: list[Any], addition: list[Any], at: int | None) -> list[Any]:
    """The list after an append that may carry a splice index.

    ``at`` is the record's ``i``. When present it is **not** where the new
    items go on the end -- it is where the existing list is cut before they are
    added. VS Code revises a response while it streams (thinking that later
    collapses, a tool call that gains its result) and re-sends the tail rather
    than the whole block list.

    Ignoring it does not lose data, it *invents* data: on the sample every
    ``i`` was below the current length, so a plain append reproduced blocks
    that had already been superseded -- 44 blocks where the conversation held
    34, the answer duplicated with stale drafts between the copies.

    One honesty note. Truncate-then-extend and replace-in-place cannot be told
    apart on the data available, because ``i + len(addition) >= len(existing)``
    held for all seven observed records. Truncation is chosen as the simpler
    reading; if a record ever arrives where they differ, this is where to look.
    """
    if at is None:
        return existing + addition
    if at < 0:
        at = max(0, len(existing) + at)
    return existing[:at] + addition


def _extend(container: Any, key: Any, value: Any, at: int | None) -> bool:
    """Append to the list at ``key``, treating a missing key as an empty list.

    A non-list ``v`` is appended as a single element. VS Code always sends a
    list, but a scalar arriving here should join the conversation rather than
    be dropped or split into characters.
    """
    addition = list(value) if isinstance(value, list) else [value]
    if isinstance(container, dict):
        existing = container.get(key)
        if existing is None:
            container[key] = _spliced([], addition, at)
            return True
        if not isinstance(existing, list):
            return False
        container[key] = _spliced(existing, addition, at)
        return True
    if isinstance(container, list):
        if not isinstance(key, int) or not -len(container) <= key < len(container):
            return False
        existing = container[key]
        if not isinstance(existing, list):
            return False
        container[key] = _spliced(existing, addition, at)
        return True
    return False


def replay(records: Iterable[Any]) -> ReplayResult:
    """Rebuild a conversation document from its records, in order.

    Never raises. A record that cannot be applied is counted and skipped, so
    one malformed line costs its own contents rather than the conversation.

    Args:
        records: Parsed records, oldest first.

    Returns:
        The rebuilt document and the counts of what was skipped.
    """
    result = ReplayResult()

    for record in records:
        if not isinstance(record, dict):
            result.unparseable += 1
            continue

        kind = record.get("kind")
        value = record.get("v")

        if kind == SNAPSHOT:
            # A later snapshot replaces everything before it -- that is what
            # makes it a snapshot. Not observed mid-file, but the format
            # permits it and honouring it costs nothing.
            result.document = dict(value) if isinstance(value, dict) else {}
            result.applied += 1
            continue

        if kind not in (SET, APPEND):
            if isinstance(kind, int):
                result.unknown_kinds[kind] = result.unknown_kinds.get(kind, 0) + 1
            else:
                result.unparseable += 1
            continue

        path = record.get("k")
        if not isinstance(path, list) or not path:
            result.bad_paths += 1
            continue

        container = _descend(result.document, path)
        if container is None:
            result.bad_paths += 1
            continue

        at = record.get("i")
        applied = (
            _assign(container, path[-1], value)
            if kind == SET
            else _extend(container, path[-1], value, at if isinstance(at, int) else None)
        )
        if applied:
            result.applied += 1
        else:
            result.bad_paths += 1

    return result


def replay_lines(lines: Iterable[str]) -> ReplayResult:
    """:func:`replay`, reading JSONL text.

    Blank lines are ignored rather than counted -- a trailing newline is not a
    lost record, and counting it would put a warning in front of the user for
    a file that is perfectly intact.
    """

    def parsed() -> Iterable[Any]:
        for line in lines:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield None  # counted as unparseable by replay()

    return replay(parsed())
