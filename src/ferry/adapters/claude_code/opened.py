"""Whether a Claude Code transcript Ferry wrote has only been opened, not used.

Claude Code appends to a conversation when it opens one. Measured on the
human's real store: a Codex conversation imported into Claude Code, opened
once, and then refused by the delete as "changed". Ferry's 158 lines were still
first, byte for byte, and exactly one line had been added after them::

    {"type": "atis-latch", "sessionId": <this conversation>, "atis": ""}

Eighty-three bytes, and no message, no answer, no tool call. The same marker is
in every one of the person's own Claude Code conversations -- 900 of them
across four -- so it is Claude Code's bookkeeping, not anything anyone said.

Only that line passes, with exactly those fields, that empty value, and this
conversation's own id. Claude Code keeps other bookkeeping of the same sort
(``ai-title``, ``custom-title``, ``last-prompt``, ``mode``), but none of it was
seen being written by *opening* a conversation, so each still counts as use
until it has been measured -- as does anything carrying a message.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final

from ferry.core.provenance import Written

__all__ = ["MARKER_KEYS", "MARKER_TYPE", "opened_not_changed"]

MARKER_TYPE: Final = "atis-latch"
MARKER_KEYS: Final = frozenset({"type", "sessionId", "atis"})


def opened_not_changed(path: Path, was: Written) -> bool:
    """Whether ``path`` is Ferry's transcript with only the opening marker after it.

    ``False`` whenever it cannot prove that: Ferry's bytes changed or gone, or
    any line after them that is not the marker -- a blank one included.
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
        added = data[was.bytes :].decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return False
    for line in added:
        try:
            record = json.loads(line)
        except ValueError:
            return False
        if not _marker(record, path.stem):
            return False
    return True


def _marker(record: Any, session: str) -> bool:
    return (
        isinstance(record, dict)
        and set(record) == MARKER_KEYS
        and record["type"] == MARKER_TYPE
        and record["sessionId"] == session
        and record["atis"] == ""
    )
