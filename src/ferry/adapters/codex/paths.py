"""Where Codex keeps its data.

Simpler than Claude Code's: no name mangling, no lossy encoding to reverse.
A conversation is a file under a date-partitioned tree, named for the thread it
belongs to, and the working directory is recorded *inside* the file rather than
in its path -- so nothing here has to be derived, only read.

Verified against Codex CLI 0.149.0-alpha.4.1 on Windows; see ``docs/FORMATS.md``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from uuid import UUID

__all__ = [
    "CODEX_HOME_ENV",
    "ROLLOUT_PATTERN",
    "attachment_dir",
    "attachment_files",
    "codex_home",
    "rollout_files",
    "rollout_name",
    "sessions_dir",
    "state_databases",
    "thread_id_of",
]

CODEX_HOME_ENV = "CODEX_HOME"
"""Relocates the whole ``.codex`` directory. Always resolve it before ``~``."""

ROLLOUT_PATTERN = re.compile(
    r"^rollout-(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(?P<thread>[0-9a-fA-F-]{36})$"
)
"""``rollout-<ISO timestamp with dashes>-<thread uuid>``.

The timestamp uses dashes where ISO 8601 uses colons, because a colon cannot
appear in a Windows filename. It is not authoritative -- the record timestamps
inside the file are -- so it is parsed only to recognise the name, never to
date the conversation.
"""


def codex_home(env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """``$CODEX_HOME`` or ``~/.codex``."""
    environ = os.environ if env is None else env
    override = environ.get(CODEX_HOME_ENV)
    return Path(override) if override else Path.home() / ".codex"


def sessions_dir(env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """The root of the date-partitioned rollout tree."""
    return codex_home(env) / "sessions"


def attachment_dir(env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """Where Codex keeps files pasted into a conversation."""
    return codex_home(env) / "attachments"


def attachment_files(
    mentioned: set[str], env: os._Environ[str] | dict[str, str] | None = None
) -> list[Path]:
    """Attachment files belonging to any of ``mentioned``.

    Codex names these only inside message prose, so the caller collects every
    UUID a conversation mentions and this resolves the ones that turn out to be
    real directories. Twelve of twenty-one such files on the probe machine had
    their contents **nowhere in any transcript** -- up to 3.4 MB each -- so an
    export that skipped them would lose real conversation material.
    """
    root = attachment_dir(env)
    if not root.is_dir():
        return []
    found: list[Path] = []
    for name in sorted(mentioned):
        directory = root / name
        if directory.is_dir():
            found.extend(sorted(p for p in directory.rglob("*") if p.is_file()))
    return found


def rollout_files(env: os._Environ[str] | dict[str, str] | None = None) -> list[Path]:
    """Every rollout transcript, oldest path first.

    Sorted by path, which is chronological given the ``YYYY/MM/DD`` layout and
    the timestamp in the filename -- so an interrupted export resumes in a
    predictable order rather than an arbitrary one.
    """
    root = sessions_dir(env)
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.rglob("*.jsonl")
        if path.is_file() and ROLLOUT_PATTERN.match(path.stem)
    )


def thread_id_of(path: Path) -> UUID | None:
    """The thread UUID a rollout belongs to, or ``None`` if the name is not one."""
    match = ROLLOUT_PATTERN.match(path.stem)
    if match is None:
        return None
    try:
        return UUID(match.group("thread"))
    except ValueError:
        return None


def rollout_name(thread_id: UUID, stamp: str) -> str:
    """The filename Codex would give this thread.

    Args:
        thread_id: The conversation's UUID.
        stamp: ``YYYY-MM-DDTHH-MM-SS``, colons already replaced.
    """
    return f"rollout-{stamp}-{thread_id}.jsonl"


def state_databases(env: os._Environ[str] | dict[str, str] | None = None) -> list[Path]:
    """The versioned state databases present, newest-looking last.

    The ``_N`` suffix is systematic across Codex (``goals_1``, ``logs_2``,
    ``memories_1``, ``state_5``) and the number moves between releases, so the
    version present is detected rather than hardcoded. ``state_5.sqlite``
    carried 46 sqlx migration rows on the probed machine -- this schema is not
    stable enough to write against blind.
    """
    home = codex_home(env)
    if not home.is_dir():
        return []
    return sorted(home.glob("state_*.sqlite"))
