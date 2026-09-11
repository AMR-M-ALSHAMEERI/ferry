"""Copies of what an import is about to overwrite.

Every adapter grew its own version of this, and all four had the same two
faults.

**A copy with no record of where it came from cannot be put back.** A file
sitting at ``~/.ferry/backups/20260825T120000Z/a1b2c3.jsonl`` says nothing
about which tool it belonged to or which directory it was taken from, so the
one moment it exists for -- the user wanting their old conversation back --
is the moment it fails them. Each backup is now written beside a
``manifest.jsonl`` line naming its original path.

**One import scattered its backups across several directories.** The stamp was
computed per file, so a run that crossed a second boundary split in two and a
run that did not put two tools' files in one flat folder. The stamp is now
taken once per Ferry run and the tool name is a directory inside it, so an
import is one folder and reads as one event.

Nothing here deletes anything. Pruning old backups is a decision for the user,
and a tool that quietly removes the copies it made of someone's conversation
history has misunderstood its job.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "BACKUP_ENV",
    "MANIFEST_NAME",
    "BackupRecord",
    "back_up",
    "backup_root",
    "read_manifest",
]

BACKUP_ENV = "FERRY_BACKUP_DIR"
"""Redirects the backups, so a test never writes into the developer's own.

Honoured with no fallback, like the provenance store's override. It became
necessary the day Ferry learned to delete: every delete takes a copy first, so
a suite that deletes things would otherwise fill the real ``~/.ferry/backups``
with copies of test fixtures.
"""

MANIFEST_NAME = "manifest.jsonl"
"""One line per copy, appended. JSONL because a crash mid-import must not cost
the record of the backups already taken."""

_run_stamp: str | None = None


def backup_root() -> Path:
    """Where backups live, unless :data:`BACKUP_ENV` says otherwise."""
    override = os.environ.get(BACKUP_ENV)
    if override:
        return Path(override)
    return Path.home() / ".ferry" / "backups"


def _stamp() -> str:
    """The timestamp for this run of Ferry, computed once.

    Deliberately not per call: an import that backs up four files should
    produce one folder the user can look at and understand, not four folders
    that happen to be adjacent.
    """
    global _run_stamp
    if _run_stamp is None:
        _run_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return _run_stamp


@dataclass(frozen=True)
class BackupRecord:
    """One copied file, as the manifest records it."""

    tool: str
    original: Path
    stored: Path
    backed_up_at: datetime


def _free_name(destination: Path) -> Path:
    """A name beside ``destination`` that is not taken.

    Reached when the same file is overwritten twice inside one run -- the
    second backup must not replace the first, which would lose the state the
    user actually started from.
    """
    if not destination.exists():
        return destination
    for suffix in range(1, 1000):
        candidate = destination.with_name(f"{destination.stem}-{suffix}{destination.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"no free filename beside {destination}")


def back_up(source: Path, tool: str, *, root: Path | None = None) -> Path:
    """Copy ``source`` into this run's backup directory and record where it came from.

    Args:
        source: The file about to be overwritten. Must exist.
        tool: The adapter's name, used as a directory so two tools' files with
            the same basename cannot collide.
        root: Override the backup root. Tests pass a temporary directory; no
            production caller should.

    Returns:
        The path the copy was written to.

    Raises:
        OSError: If the copy fails. The caller must treat that as a reason
            **not to overwrite** -- an import that cannot take a backup has no
            business destroying the thing it was backing up.
    """
    directory = (backup_root() if root is None else root) / _stamp() / tool
    directory.mkdir(parents=True, exist_ok=True)
    destination = _free_name(directory / source.name)
    shutil.copy2(source, destination)

    moment = datetime.now(UTC)
    line = json.dumps(
        {
            "backed_up_at": moment.isoformat().replace("+00:00", "Z"),
            "tool": tool,
            "original": str(source),
            "stored": destination.name,
        }
    )
    manifest = directory.parent / MANIFEST_NAME
    try:
        with manifest.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
    except OSError:
        # The copy is the thing that matters and it already exists. Losing the
        # note about it is a smaller harm than failing an import over it.
        pass
    return destination


def read_manifest(run_dir: Path) -> list[BackupRecord]:
    """Every copy recorded in one backup directory, oldest first.

    A line that will not parse is skipped rather than failing the read: this is
    what someone consults when they want a file back, and one bad line must not
    hide the rest.
    """
    manifest = run_dir / MANIFEST_NAME
    records: list[BackupRecord] = []
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:
        return records
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            moment = datetime.fromisoformat(str(raw["backed_up_at"]).replace("Z", "+00:00"))
            tool = str(raw["tool"])
            records.append(
                BackupRecord(
                    tool=tool,
                    original=Path(str(raw["original"])),
                    stored=run_dir / tool / str(raw["stored"]),
                    backed_up_at=moment,
                )
            )
        except (ValueError, KeyError, TypeError):
            continue
    return records
