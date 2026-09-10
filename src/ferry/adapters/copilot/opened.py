"""Whether a Copilot transcript Ferry wrote has only been opened, not used.

VS Code does not leave a transcript alone when it shows one. Ferry writes a
single ``kind: 0`` snapshot; opening the conversation appends a ``kind: 1``
line for every field VS Code fills in on the requests it has just loaded.
Measured on the human's real store, a conversation imported and opened once to
check it had arrived had grown from one line to 1,242, and the delete refused
it as "changed". Of those lines:

- Ferry's snapshot was still first, byte for byte;
- all 1,241 after it were ``kind: 1`` sets;
- **not one appended a request, or touched a message or a response.**

What they set, with the only values seen, is the whole of what opening may
have done:

===========================  =====================================
the document                 ``hasPendingEdits`` false,
                             ``pendingRequests`` empty,
                             ``initialLocation``, ``responderUsername``
each request Ferry wrote     ``elapsedMs``, ``timeSpentWaiting``,
                             ``responseTimestamp`` (numbers),
                             ``modelState``, ``variableData`` (objects),
                             ``hiddenFromTranscript`` false,
                             ``codeCitations``, ``contentReferences``,
                             ``responseMarkdownInfo`` empty
===========================  =====================================

Carrying a conversation on appends a request (``kind: 2``), and anything else --
a field not in that table, a value outside it, a request Ferry did not write, a
fresh snapshot -- counts as use. It is a proof in the same sense as
Antigravity's: Ferry's bytes are checked against the recorded checksum, and
everything after them is read line by line rather than guessed at.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from ferry.adapters.copilot.deltas import SET
from ferry.core.provenance import Written

__all__ = ["DOCUMENT_FIELDS", "REQUEST_FIELDS", "opened_not_changed"]


def _empty(value: Any) -> bool:
    return isinstance(value, list) and not value


def _false(value: Any) -> bool:
    return value is False


def _text(value: Any) -> bool:
    return isinstance(value, str)


def _number(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _object(value: Any) -> bool:
    return isinstance(value, dict)


DOCUMENT_FIELDS: Final[dict[str, Callable[[Any], bool]]] = {
    "hasPendingEdits": _false,
    "initialLocation": _text,
    "pendingRequests": _empty,
    "responderUsername": _text,
}
"""Fields on the conversation itself that opening it sets, and the values allowed."""

REQUEST_FIELDS: Final[dict[str, Callable[[Any], bool]]] = {
    "codeCitations": _empty,
    "contentReferences": _empty,
    "elapsedMs": _number,
    "hiddenFromTranscript": _false,
    "modelState": _object,
    "responseMarkdownInfo": _empty,
    "responseTimestamp": _number,
    "timeSpentWaiting": _number,
    "variableData": _object,
}
"""Fields on each request Ferry wrote that opening it sets, and the values allowed."""


def opened_not_changed(path: Path, was: Written) -> bool:
    """Whether ``path`` is Ferry's transcript with only VS Code's bookkeeping after it.

    ``False`` whenever it cannot prove that: Ferry's bytes changed or gone, a
    line that is not JSON, or any line outside the measured table.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if len(data) <= was.bytes:
        return False
    if hashlib.sha256(data[: was.bytes]).hexdigest() != was.sha256:
        return False
    try:
        snapshot = json.loads(data[: was.bytes].decode("utf-8"))
        added = data[was.bytes :].decode("utf-8").splitlines()
    except (UnicodeDecodeError, ValueError):
        return False

    document = snapshot.get("v") if isinstance(snapshot, dict) else None
    requests = document.get("requests") if isinstance(document, dict) else None
    if not isinstance(requests, list):
        return False

    # Every line, blank ones included. VS Code was not measured writing a blank
    # line, and a proof that skipped what it had not seen would be a guess.
    for line in added:
        try:
            record = json.loads(line)
        except ValueError:
            return False
        if not _bookkeeping(record, len(requests)):
            return False
    return True


def _bookkeeping(record: Any, requests: int) -> bool:
    """Whether one appended line is a field opening sets, on something Ferry wrote."""
    if not isinstance(record, dict) or record.get("kind") != SET:
        return False
    keys, value = record.get("k"), record.get("v")
    if not isinstance(keys, list):
        return False
    if len(keys) == 1 and keys[0] in DOCUMENT_FIELDS:
        return DOCUMENT_FIELDS[keys[0]](value)
    if (
        len(keys) == 3
        and keys[0] == "requests"
        and type(keys[1]) is int
        and 0 <= keys[1] < requests
        and keys[2] in REQUEST_FIELDS
    ):
        return REQUEST_FIELDS[keys[2]](value)
    return False
