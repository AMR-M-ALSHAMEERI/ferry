"""Opening a Claude Code conversation is not using it.

Found by the human: a Codex conversation imported into Claude Code, opened, and
then refused by the delete as "changed". Claude Code had appended one line -
``{"type": "atis-latch", "sessionId": ..., "atis": ""}`` - after Ferry's 158,
which were untouched. The same marker is in every one of their own Claude Code
conversations.

Only that exact line passes. Everything in ``USED`` must still count as use.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ferry.adapters.base import RemoveOptions
from ferry.adapters.removal import remove, survey
from tests.test_removal import a_conversation, claude_code, import_into


def marker(session: str) -> dict[str, Any]:
    return {"type": "atis-latch", "sessionId": session, "atis": ""}


def append(path: Path, *rows: dict[str, Any] | str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write((row if isinstance(row, str) else json.dumps(row)) + "\n")


USED: dict[str, Callable[[str], dict[str, Any] | str]] = {
    "a marker for another conversation": lambda s: marker("someone-else"),
    "a marker carrying a value": lambda s: {**marker(s), "atis": "x"},
    "a marker with a field more": lambda s: {**marker(s), "extra": 1},
    "a message": lambda s: {
        "type": "user",
        "sessionId": s,
        "message": {"role": "user", "content": [{"type": "text", "text": "and then?"}]},
    },
    "a title never measured on opening": lambda s: {
        "type": "ai-title",
        "sessionId": s,
        "aiTitle": "t",
    },
    "a blank line": lambda s: "",
}


def test_opening_it_to_look_is_not_using_it(tmp_path: Path) -> None:
    target = claude_code(tmp_path)
    [path] = import_into(target, tmp_path, a_conversation("claude-code"))
    append(path, marker(path.stem))

    [found] = survey(target.adapter)
    list(remove(target.adapter, RemoveOptions()))

    assert found.removable
    assert target.written() == []


@pytest.mark.parametrize("what", sorted(USED))
def test_anything_more_than_the_marker_still_counts_as_use(what: str, tmp_path: Path) -> None:
    target = claude_code(tmp_path)
    [path] = import_into(target, tmp_path, a_conversation("claude-code"))
    append(path, marker(path.stem), USED[what](path.stem))

    [found] = survey(target.adapter)
    list(remove(target.adapter, RemoveOptions()))

    assert found.state == "changed"
    assert path.is_file()


def test_ferrys_own_lines_changed_still_count_as_use(tmp_path: Path) -> None:
    target = claude_code(tmp_path)
    [path] = import_into(target, tmp_path, a_conversation("claude-code"))
    path.write_bytes(path.read_bytes().replace(b'"type"', b'"typE"', 1))
    append(path, marker(path.stem))

    [found] = survey(target.adapter)

    assert found.state == "changed"
