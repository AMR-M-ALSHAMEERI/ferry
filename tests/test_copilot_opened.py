"""Opening a Copilot conversation in VS Code is not using it.

Found by the human: a conversation imported into Copilot, opened to check it
had arrived, and then refused by the delete as "changed". On their real store
VS Code had appended 1,241 lines of bookkeeping to Ferry's one - timestamps,
model state, empty citation lists - and not one new request or changed answer.

The lines appended here are the kinds measured there. Everything in
``USED`` is something opening did not do, and each must still be refused.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ferry.adapters.base import RemoveOptions
from ferry.adapters.copilot.deltas import APPEND, SET, SNAPSHOT
from ferry.adapters.removal import remove, survey
from tests.test_removal import a_conversation, copilot, import_into


@pytest.fixture(autouse=True)
def _vs_code_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whether VS Code is open on the machine running the suite must not decide a result."""
    monkeypatch.setattr("ferry.adapters.copilot.adapter.vs_code_is_running", lambda env=None: False)


OPENED: list[tuple[int, list[Any], Any]] = [
    (SET, ["initialLocation"], "panel"),
    (SET, ["responderUsername"], "GitHub Copilot"),
    (SET, ["hasPendingEdits"], False),
    (SET, ["requests", 0, "hiddenFromTranscript"], False),
    (SET, ["requests", 0, "responseTimestamp"], 1788000000000),
    (SET, ["requests", 0, "responseMarkdownInfo"], []),
    (SET, ["requests", 0, "modelState"], {"completedAt": 1788000000000, "value": 1}),
    (SET, ["requests", 0, "contentReferences"], []),
    (SET, ["requests", 0, "codeCitations"], []),
    (SET, ["requests", 0, "timeSpentWaiting"], 0),
    (SET, ["requests", 0, "elapsedMs"], 1200),
    (SET, ["requests", 0, "variableData"], {"variables": []}),
    (SET, ["pendingRequests"], []),
]
"""What VS Code appended on opening, in the order it did, one request's worth."""

USED: dict[str, tuple[int, list[Any] | None, Any]] = {
    "a new question": (APPEND, ["requests"], [{"message": {"text": "and then?"}}]),
    "a changed answer": (SET, ["requests", 0, "response"], [{"value": "different"}]),
    "a question waiting to be sent": (SET, ["pendingRequests"], [{"message": "x"}]),
    "edits waiting to be applied": (SET, ["hasPendingEdits"], True),
    "a request hidden from the transcript": (SET, ["requests", 0, "hiddenFromTranscript"], True),
    "a citation added": (SET, ["requests", 0, "codeCitations"], [{"uri": "x"}]),
    "a field never measured": (SET, ["requests", 0, "somethingNew"], 1),
    "a request Ferry did not write": (SET, ["requests", 5, "elapsedMs"], 1),
    "a fresh snapshot": (SNAPSHOT, None, {"requests": []}),
}


def append(path: Path, *lines: tuple[int, list[Any] | None, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for kind, keys, value in lines:
            handle.write(json.dumps({"kind": kind, "k": keys, "v": value}) + "\n")


def test_opening_it_to_look_is_not_using_it(tmp_path: Path) -> None:
    target = copilot(tmp_path)
    item = a_conversation("copilot")
    [path] = import_into(target, tmp_path, item)
    append(path, *OPENED)

    [found] = survey(target.adapter)
    list(remove(target.adapter, RemoveOptions()))

    assert found.removable
    assert target.written() == []
    assert target.listed is not None
    assert not target.listed(item.id)


@pytest.mark.parametrize("what", sorted(USED))
def test_anything_more_than_opening_still_counts_as_use(what: str, tmp_path: Path) -> None:
    target = copilot(tmp_path)
    [path] = import_into(target, tmp_path, a_conversation("copilot"))
    append(path, *OPENED, USED[what])

    [found] = survey(target.adapter)
    list(remove(target.adapter, RemoveOptions()))

    assert found.state == "changed"
    assert path.is_file()


def test_ferrys_own_line_changed_still_counts_as_use(tmp_path: Path) -> None:
    """The bookkeeping only counts as opening if what it follows is Ferry's."""
    target = copilot(tmp_path)
    [path] = import_into(target, tmp_path, a_conversation("copilot"))
    path.write_bytes(path.read_bytes().replace(b'"version"', b'"versioN"', 1))
    append(path, *OPENED)

    [found] = survey(target.adapter)

    assert found.state == "changed"
