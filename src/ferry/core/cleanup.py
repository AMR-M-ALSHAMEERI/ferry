"""Clearing out the backups Ferry has taken, when the person chooses to.

Every import that replaces something, and every delete, copies what it is about
to destroy into ``~/.ferry/backups`` first. Nothing prunes those copies on its
own: a tool that quietly removed what it saved of someone's history would have
misunderstood its job. That leaves a folder that only grows, and no way to tell
which of 141 timestamps is safe to lose. This is that way -- each backup listed
with what it holds, and removed only when chosen.

**Only folders Ferry made.** A backup is a directory directly inside the backup
root, named by the timestamp Ferry gives it (``20260910T232704Z``). Anything else
there -- a file, a folder someone put there by hand -- is never listed and never
deleted, the same rule a bundle delete follows in refusing a directory without a
manifest.

**What a copy is called** is read from the backup's own manifest, which records
where each copy came from:

``temporary folders``
    The original was in the system temp folder or a pytest folder. A copy of a
    file that only ever lived there was never part of anyone's history. Ferry's
    own test suite left most of these, before ``FERRY_BACKUP_DIR`` existed.
``a bundle``
    Taken when a conversation was deleted from a bundle in Inspect.
``Claude Code settings``
    ``.claude.json``, taken before Ferry let Claude Code open a folder.
otherwise
    The assistant it came from.

A backup is offered as leftover only when **every** copy in it is from a
temporary folder. One real copy among forty temporary ones makes it the
person's to decide about.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from ferry.core.backup import MANIFEST_NAME, BackupRecord, backup_root, read_manifest

__all__ = [
    "BUNDLE",
    "SETTINGS",
    "TEMPORARY",
    "BackupRun",
    "Cleared",
    "delete_runs",
    "list_runs",
    "newest_by_tool",
]

RUN_NAME: Final = re.compile(r"^\d{8}T\d{6}Z$")
"""How Ferry names a backup folder. Nothing else is ever listed or deleted."""

TEMPORARY: Final = "temporary folders"
BUNDLE: Final = "a bundle"
SETTINGS: Final = "Claude Code settings"
UNRECORDED: Final = "nothing recorded"


@dataclass(frozen=True)
class BackupRun:
    """One backup folder: everything a single run of Ferry copied."""

    path: Path
    taken_at: datetime
    size: int
    copies: int
    holds: tuple[str, ...]
    """What its copies are, as the screen names them, sorted."""

    tools: tuple[str, ...] = field(default=())
    """Assistants it holds real copies from, for "the newest backup for Codex"."""

    @property
    def from_temporary(self) -> bool:
        """Whether every copy in it came from a temporary folder."""
        return self.holds == (TEMPORARY,)


@dataclass(frozen=True)
class Cleared:
    """What a delete did."""

    deleted: int = 0
    freed: int = 0
    problems: tuple[str, ...] = ()


def _temporary(original: Path) -> bool:
    if any(part.lower().startswith(("pytest-", ".pytest")) for part in original.parts):
        return True
    return original.is_relative_to(Path(tempfile.gettempdir()))


def _label(record: BackupRecord) -> str:
    if _temporary(record.original):
        return TEMPORARY
    if record.tool.startswith("bundle-"):
        return BUNDLE
    if record.original.name == ".claude.json":
        return SETTINGS
    return record.tool


def _is_run(path: Path, root: Path) -> bool:
    return (
        path.parent == root
        and RUN_NAME.match(path.name) is not None
        and path.is_dir()
        and not path.is_symlink()
    )


def list_runs(root: Path | None = None) -> list[BackupRun]:
    """Every backup folder Ferry made, newest first."""
    base = backup_root() if root is None else root
    if not base.is_dir():
        return []
    runs: list[BackupRun] = []
    for path in base.iterdir():
        if not _is_run(path, base):
            continue
        try:
            files = [item for item in path.rglob("*") if item.is_file()]
            size = sum(item.stat().st_size for item in files)
        except OSError:
            continue
        records = read_manifest(path)
        labels = sorted({_label(record) for record in records}) or [UNRECORDED]
        tools = sorted({record.tool for record in records if _label(record) == record.tool})
        runs.append(
            BackupRun(
                path=path,
                taken_at=datetime.strptime(path.name, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC),
                size=size,
                copies=sum(1 for item in files if item.name != MANIFEST_NAME),
                holds=tuple(labels),
                tools=tuple(tools),
            )
        )
    runs.sort(key=lambda run: run.taken_at, reverse=True)
    return runs


def newest_by_tool(runs: Iterable[BackupRun]) -> dict[str, BackupRun]:
    """The most recent backup holding each assistant's files."""
    newest: dict[str, BackupRun] = {}
    for run in sorted(runs, key=lambda item: item.taken_at, reverse=True):
        for tool in run.tools:
            newest.setdefault(tool, run)
    return newest


def _writable(path: Path) -> None:
    """Clear read-only flags, which ``copy2`` carries over from the original."""
    for item in [path, *path.rglob("*")]:
        try:
            os.chmod(item, stat.S_IREAD | stat.S_IWRITE | (stat.S_IEXEC if item.is_dir() else 0))
        except OSError:
            continue


def delete_runs(runs: Sequence[BackupRun], root: Path | None = None) -> Cleared:
    """Delete these backup folders, and nothing that is not one.

    Each is checked again rather than trusted: directly inside the backup root,
    named as Ferry names them, a real directory and not a link to one.
    """
    base = backup_root() if root is None else root
    deleted = freed = 0
    problems: list[str] = []
    for run in runs:
        if not _is_run(run.path, base):
            problems.append(f"{run.path.name} is not a backup Ferry made, so it was left alone")
            continue
        try:
            shutil.rmtree(run.path)
        except OSError:
            _writable(run.path)
            try:
                shutil.rmtree(run.path)
            except OSError as exc:
                problems.append(f"could not delete {run.path.name}: {exc}")
                continue
        deleted += 1
        freed += run.size
    return Cleared(deleted=deleted, freed=freed, problems=tuple(problems))
