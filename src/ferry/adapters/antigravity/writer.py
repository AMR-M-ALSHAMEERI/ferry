"""Writing a conversation back into Antigravity, with its paths corrected.

An Antigravity-to-Antigravity import is a copy of the original database with
every embedded path rewritten. Not a reconstruction from UCS: Ferry has no
schema for these blobs, so rebuilding one would mean inventing the parts it
could not read.

Three things here are less obvious than they look.

**The snapshot is taken through SQLite, not the filesystem.** These databases
run in WAL mode, so the newest steps of an open conversation are in the
``-wal`` file and not in the ``.db`` at all. Copying the file would export a
conversation missing its most recent messages -- the ones the user is most
likely to care about -- and would look completely successful.

**Blob columns are discovered, not listed.** The plan names six. Asking the
database which columns hold blobs finds whatever this version actually has, so
a column added in a later release gets its paths rewritten instead of silently
keeping a path to a directory that does not exist on this machine.

**Rows are addressed by ``rowid``.** ``steps`` is keyed by ``idx``, but not
every table here is keyed the same way and one has a text primary key. ``rowid``
is the one identifier every ordinary SQLite table has.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ferry.adapters.antigravity import paths
from ferry.adapters.antigravity.remap import PathRemapper
from ferry.adapters.antigravity.wire import rewrite

__all__ = [
    "RewriteReport",
    "antigravity_is_running",
    "blob_columns",
    "remap_database",
    "snapshot",
    "write_project",
]


@dataclass
class RewriteReport:
    """What a path rewrite touched."""

    rows_seen: int = 0
    blobs_seen: int = 0
    blobs_changed: int = 0
    columns: dict[str, int] = field(default_factory=dict)
    """Blobs changed, per ``table.column``."""

    unparseable: int = 0
    """Blobs that are not protobuf and were carried through untouched.

    Not an error. A blob this codec declines to parse is left exactly as it
    was, which is the safe outcome -- the alternative is guessing at its
    structure and writing back something different.
    """


def snapshot(database: Path, destination: Path) -> None:
    """A complete, consistent copy of a conversation database.

    Uses SQLite's own backup, which folds the WAL in, so the copy holds every
    step including ones not yet written to the main file. The source is opened
    read-only; the user's database is never written to.

    Raises:
        sqlite3.Error: If the database cannot be read.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    with paths.open_readonly(database) as source:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()


def blob_columns(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every ``(table, column)`` declared as a blob, in a stable order."""
    found: list[tuple[str, str]] = []
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
        if not str(row[0]).startswith("sqlite_")
    ]
    for table in tables:
        for row in connection.execute(f'PRAGMA table_info("{table}")'):
            if str(row[2]).strip().lower() == "blob":
                found.append((table, str(row[1])))
    return found


def remap_database(database: Path, remapper: PathRemapper) -> RewriteReport:
    """Rewrite every path inside a database, in place.

    Meant for a copy Ferry made, never for the user's own file. The whole
    rewrite runs in one transaction, so a failure part-way leaves the copy as
    it was rather than half-remapped -- a half-remapped conversation opens
    normally and is wrong in ways nobody would notice.
    """
    report = RewriteReport()
    if not remapper.active:
        return report

    connection = sqlite3.connect(database)
    try:
        columns = blob_columns(connection)
        with connection:
            for table, column in columns:
                rows = connection.execute(
                    f'SELECT rowid, "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
                ).fetchall()
                for rowid, value in rows:
                    report.rows_seen += 1
                    if not isinstance(value, bytes | bytearray) or not value:
                        continue
                    blob = bytes(value)
                    report.blobs_seen += 1
                    changed = rewrite(blob, remapper.field)
                    if changed is blob:
                        continue
                    connection.execute(
                        f'UPDATE "{table}" SET "{column}" = ? WHERE rowid = ?', (changed, rowid)
                    )
                    report.blobs_changed += 1
                    key = f"{table}.{column}"
                    report.columns[key] = report.columns.get(key, 0) + 1
    finally:
        connection.close()
    return report


def write_project(
    document: dict[str, object],
    remapper: PathRemapper,
    env: os._Environ[str] | dict[str, str] | None = None,
) -> Path | None:
    """Put a project record in place, with its folder remapped.

    A conversation names a project, and a conversation whose project does not
    exist here is one Antigravity has nowhere to file. **An existing record is
    left alone**: it describes a folder on *this* machine, which the bundle's
    copy does not, and overwriting it would re-point every conversation already
    filed under it.
    """
    identifier = document.get("id")
    if not isinstance(identifier, str) or not identifier:
        return None

    directory = paths.projects_dir(env)
    destination = directory / f"{identifier}.json"
    if destination.exists():
        return destination

    remapped = json.loads(remapper.text(json.dumps(document))) if remapper.active else document
    directory.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".json.ferry-tmp")
    temporary.write_text(json.dumps(remapped, indent=2), encoding="utf-8")
    temporary.replace(destination)
    return destination


_PROCESS_NAMES = ("antigravity",)


def antigravity_is_running(env: os._Environ[str] | dict[str, str] | None = None) -> bool:
    """Whether Antigravity looks to be running.

    A hint, not a lock. Ferry warns on it rather than refusing, because the
    check is crude enough to be wrong in both directions and the real
    protection is that a write to a database SQLite has locked fails loudly
    instead of reporting a success the app would then overwrite.
    """
    try:
        if sys.platform == "win32":
            output = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["tasklist", "/fo", "csv", "/nh"],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout
        else:
            output = subprocess.run(  # noqa: S603
                ["ps", "-A", "-o", "comm="],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    lowered = output.lower()
    return any(name in lowered for name in _PROCESS_NAMES)
