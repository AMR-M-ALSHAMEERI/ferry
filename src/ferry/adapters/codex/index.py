r"""The row that makes a written conversation findable.

**A rollout file with no row in ``threads`` is invisible**, and this project has
now learned that in three different tools: a Copilot transcript missing from the
chat index, a Claude Code conversation in an untrusted folder, and here. The
file is intact, the words are all present, and the tool's list is empty --
indistinguishable from the import having silently failed.

Measured on Codex 0.147 rather than assumed. Ten rollouts on disk against six
rows: the six were the ones the picker offered. ``codex resume <id>`` finds a
conversation with no row, which is how Phase 3 was verified, but nobody
migrating their history knows the id -- **they open the picker.**

This retires an older note that recorded a rebuilt rollout resuming with
``threads`` empty. That was true at Codex 0.98.

Two settings here are **security decisions, not formatting**, and both are
written at the cautious end:

``approval_mode``
    ``on-request``, never ``never``. Both appear in real rows, and ``never``
    means the session stops asking before it acts. Ferry is creating a session
    on someone's behalf out of a conversation that happened elsewhere; choosing
    the setting that gives away their approval would be Ferry deciding
    something it was never asked to decide.

``sandbox_policy``
    The most restricted policy observed: the filesystem readable, nothing
    writable, network restricted. A migrated transcript needs no permissions at
    all -- it is a record of work already done -- and anything the person wants
    to grant afterwards, they can grant knowingly.

**And ``cwd`` has to be spelt the way Codex spells it.** The CLI picker offers
the sessions belonging to the folder you are standing in, and it matches on the
stored string. Codex writes a Windows working directory in the extended-length
form -- ``\\?\C:\...`` -- in **7 of 7** rows measured, its own new
sessions included. Ferry wrote a plain ``C:\...``, which is the same directory
and a different string, so the picker matched nothing and showed an empty list
over four conversations that were complete on disk and correctly indexed.

Confirmed by changing one row and watching the conversation appear. **The
fourth time this project has met the same shape of gate**, after Copilot's chat
index, Claude Code's trusted folder, and the missing ``threads`` row above -- and
the first where the row existed and was still not enough.

``rollout_path`` is deliberately left alone: Codex's own rows carry both
spellings, so there is no measured form to match.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID

__all__ = [
    "APPROVAL_MODE",
    "canonical_cwd",
    "drop_thread_row",
    "SANDBOX_POLICY",
    "ThreadIndexLocked",
    "thread_row",
    "upsert_thread_row",
]

_BUSY_TIMEOUT_SECONDS: Final = 5.0

APPROVAL_MODE: Final = "on-request"
"""Ask before acting. See the module docstring: this is a safety choice."""

SANDBOX_POLICY: Final = json.dumps(
    {
        "type": "managed",
        "file_system": {
            "type": "restricted",
            "entries": [{"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"}],
        },
        "network": "restricted",
    },
    separators=(",", ":"),
)
"""Read the filesystem, write nothing, no network. The narrowest real policy."""

COLUMNS: Final = (
    "id",
    "rollout_path",
    "created_at",
    "updated_at",
    "source",
    "model_provider",
    "cwd",
    "title",
    "sandbox_policy",
    "approval_mode",
    "tokens_used",
    "has_user_event",
    "archived",
    "cli_version",
    "first_user_message",
    "memory_mode",
    "created_at_ms",
    "updated_at_ms",
    "thread_source",
    "preview",
    "recency_at",
    "recency_at_ms",
    "history_mode",
    "is_pinned",
)
"""Every NOT NULL column, and the ones every real row fills.

Deliberately not the whole table. ``model``, ``reasoning_effort`` and the
``git_*`` columns are nullable and describe a session that ran here; filling
them would be asserting a model and a git state for work done in another tool.
"""


class ThreadIndexLocked(RuntimeError):
    """The index could not be written, almost always because Codex is running.

    Raised rather than retried. A running Codex holds this state and rewrites it
    on exit, so even a write that succeeded would be discarded -- and reporting
    success would be a lie the person only discovers when the list is empty.
    """


EXTENDED_PREFIX: Final = "\\\\?\\"
"""How Windows spells an absolute path when it wants no length limit."""


def canonical_cwd(cwd: str) -> str:
    r"""A working directory spelt the way Codex spells it, so the picker matches.

    Only a drive-letter path is touched, and the shape is the test rather than
    the running platform: a ``C:\...`` string means the same thing whichever
    machine reads the row, and a rule written against `os.name` would go
    unexercised on eleven of the twelve CI legs.
    """
    if cwd.startswith(EXTENDED_PREFIX):
        return cwd
    drive_letter = len(cwd) >= 3 and cwd[0].isalpha() and cwd[1] == ":" and cwd[2] in ("/", "\\")
    if not drive_letter:
        return cwd
    return EXTENDED_PREFIX + cwd.replace("/", "\\")


def _first_line(text: str, limit: int = 200) -> str:
    line = " ".join(text.split())
    return line[:limit]


def thread_row(
    *,
    conversation_id: UUID,
    rollout_path: Path,
    cwd: str,
    title: str,
    first_message: str,
    created_at: datetime,
    updated_at: datetime,
    cli_version: str,
) -> dict[str, Any]:
    """One row of the picker, from the conversation and nothing else."""
    created = int(created_at.astimezone(UTC).timestamp())
    updated = int(updated_at.astimezone(UTC).timestamp())
    shown = _first_line(title) or "Imported conversation"
    return {
        "id": str(conversation_id),
        "rollout_path": str(rollout_path),
        "created_at": created,
        "updated_at": updated,
        # Where the session came from, as Codex records it. "cli" is what the
        # rebuilt header says wrote the rollout, and the two must agree.
        "source": "cli",
        "model_provider": "openai",
        "cwd": canonical_cwd(cwd),
        "title": shown,
        "sandbox_policy": SANDBOX_POLICY,
        "approval_mode": APPROVAL_MODE,
        "tokens_used": 0,
        "has_user_event": 1,
        "archived": 0,
        "cli_version": cli_version,
        "first_user_message": _first_line(first_message),
        "memory_mode": "enabled",
        "created_at_ms": created * 1000,
        "updated_at_ms": updated * 1000,
        "thread_source": "user",
        "preview": _first_line(first_message),
        "recency_at": updated,
        "recency_at_ms": updated * 1000,
        "history_mode": "legacy",
        "is_pinned": 0,
    }


def upsert_thread_row(database: Path, row: dict[str, Any]) -> None:
    """Add or replace one conversation in the picker's index.

    Unlike the Copilot chat index, this is a table of rows rather than one JSON
    blob holding everything, so a careless write cannot take the neighbours with
    it. The table is **not** created when absent: a missing ``state_*.sqlite``
    means this is not a Codex installation Ferry understands, and inventing
    Codex's schema for it would be guessing at another program's storage.
    """
    if not database.is_file():
        raise ThreadIndexLocked(f"no Codex state database at {database}")
    try:
        connection = sqlite3.connect(database, timeout=_BUSY_TIMEOUT_SECONDS)
    except sqlite3.Error as exc:  # pragma: no cover - depends on the filesystem
        raise ThreadIndexLocked(f"cannot open {database.name}: {exc}") from exc
    try:
        with connection:
            connection.execute(
                f"INSERT OR REPLACE INTO threads ({','.join(COLUMNS)}) "
                f"VALUES ({','.join('?' * len(COLUMNS))})",
                tuple(row[name] for name in COLUMNS),
            )
    except sqlite3.OperationalError as exc:
        raise ThreadIndexLocked(
            f"{database.name} is locked - close Codex and try again ({exc})"
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise ThreadIndexLocked(f"{database.name} could not be written: {exc}") from exc
    finally:
        connection.close()


def drop_thread_row(database: Path, thread_id: UUID) -> bool:
    """Take one conversation out of the picker's index. Returns whether it was listed.

    One row, by id. Every other row in the table is a session somebody ran, and
    nothing here reads or rewrites them. A missing database lists nothing, which
    is the state being asked for, so that is not an error either.

    Raises:
        ThreadIndexLocked: If the row could not be deleted.
    """
    if not database.is_file():
        return False
    try:
        connection = sqlite3.connect(database, timeout=_BUSY_TIMEOUT_SECONDS)
    except sqlite3.Error as exc:  # pragma: no cover - depends on the filesystem
        raise ThreadIndexLocked(f"cannot open {database.name}: {exc}") from exc
    try:
        with connection:
            cursor = connection.execute("DELETE FROM threads WHERE id = ?", (str(thread_id),))
            return cursor.rowcount > 0
    except sqlite3.OperationalError as exc:
        raise ThreadIndexLocked(
            f"{database.name} is locked - close Codex and try again ({exc})"
        ) from exc
    except sqlite3.DatabaseError as exc:
        raise ThreadIndexLocked(f"{database.name} could not be written: {exc}") from exc
    finally:
        connection.close()
