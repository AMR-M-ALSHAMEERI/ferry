"""Whether a Codex rollout Ferry wrote has only been opened, not used.

Codex re-files an older rollout in its current format when it opens one. The
human imported a conversation, opened it in Codex, and the delete refused it as
"changed". Measured by rebuilding Ferry's original from the bundle (checksum
equal to the one recorded) and comparing it with the file Codex left:

- **all 774 lines rewritten**, none byte-identical, and still 774 of them;
- every line had gained an ``ordinal``;
- 120 display events -- ``agent_message`` 50, ``agent_reasoning`` 65,
  ``user_message`` 5 -- had become ``item_completed``;
- ``session_meta`` had gained ``base_instructions`` and ``history_mode``;
- and **every timestamp was unchanged, and all 349 ``response_item`` records --
  the model's copy of the conversation -- were identical field for field.**

No checksum of the bytes survives that, so a Codex record carries a second one
(:attr:`~ferry.core.provenance.Written.content`), taken at import over exactly
what the rewrite was measured to keep: the timestamp of every line, in order,
and every ``response_item`` payload. Carrying a conversation on adds lines, so
it adds timestamps and model records, and either changes this.

A record written before this existed has no second checksum. It stays
"changed" -- there is nothing to prove against, and without proof a changed
file is the person's.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ferry.core.provenance import Written

__all__ = ["content_fingerprint", "opened_not_changed"]


def _canon(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def content_fingerprint(path: Path) -> str | None:
    """A checksum of what Codex keeps when it rewrites a rollout, or ``None``.

    Read a line at a time: a rollout can be tens of megabytes. ``None`` for a
    file that cannot be read or has a line that is not a JSON object, because
    then there is no telling what it holds.
    """
    digest = hashlib.sha256()
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                # Neither Ferry nor Codex's rewrite was seen writing a blank
                # line, and a proof that skipped what it had not seen would be
                # a guess -- the mistake Copilot's first draft made (#231).
                if not line.strip():
                    return None
                record = json.loads(line)
                if not isinstance(record, dict):
                    return None
                digest.update(b"T" + _canon(record.get("timestamp")) + b"\n")
                if record.get("type") == "response_item":
                    digest.update(b"P" + _canon(record.get("payload")) + b"\n")
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return digest.hexdigest()


def opened_not_changed(path: Path, was: Written) -> bool:
    """Whether the conversation Ferry wrote is all still there, and nothing more."""
    return was.content is not None and content_fingerprint(path) == was.content
