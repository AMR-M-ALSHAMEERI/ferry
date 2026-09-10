"""Deleting what Ferry wrote into a tool, and nothing else.

Every import records two things about a converted conversation: the file it
wrote, and the SHA-256 of what it wrote (:mod:`ferry.core.provenance`). That is
what makes a delete safe to offer, because it answers the one question that
matters before anything leaves someone's history: *is this still exactly what
Ferry put there, or has somebody worked in it since?*

A conversation that was migrated and then carried on holds real work. Deleting
it because Ferry once created it would destroy exactly what the tool is for, so
only one of these states is ever removed:

``removable``  byte for byte what Ferry wrote, and nothing beside it says
               otherwise.
``changed``    different now. It is the person's, and it stays.
``gone``       no longer where Ferry put it.
``unknown``    no fingerprint on record, so Ferry cannot tell. It stays, and
               the screen says why.
``elsewhere``  written into a different store from the one being cleaned.

**What it covers.** Only conversations Ferry *converted* from another tool,
because those are the only ones it keeps a record of. A restore puts back a
person's own conversation, and deleting that is not Ferry's business.

**Both halves of the write.** Three of the four targets list a conversation
somewhere other than its file -- Copilot's chat index, Codex's ``threads``
table, Antigravity's ``agyhub_summaries_proto.pb`` -- and a delete that took
only the file would leave an empty conversation in the list. So the entry goes
first, then the file, then Ferry's record of it. Stopping anywhere leaves
something a second run finishes: taking out an entry that is already gone does
nothing, and the record stays until the file has gone.

Claude Code has no such entry. What it has is a list of folders it may open,
which an import may have added to; that list is shared with every other
conversation in those folders, so it is left as it is.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal
from uuid import UUID

from ferry.adapters.base import Adapter, RemovalBlocked, RemoveEvent, RemoveOptions
from ferry.adapters.census import count_of
from ferry.core import provenance
from ferry.core.backup import back_up

__all__ = ["WHY_IT_STAYS", "Candidate", "State", "remove", "survey"]

State = Literal["removable", "changed", "gone", "unknown", "elsewhere"]

WHY_IT_STAYS: Final[dict[str, str]] = {
    "changed": "changed since Ferry wrote it, so it is yours now",
    "gone": "no longer where Ferry put it",
    "unknown": "imported before Ferry kept fingerprints, so it cannot tell if you used it",
    "elsewhere": "written into a different store from this one",
}
"""Why a conversation Ferry imported is not offered, in the person's terms."""


@dataclass(frozen=True)
class Candidate:
    """One conversation Ferry converted into a tool, and whether it can go."""

    record_id: UUID
    state: State
    path: Path | None
    came_from: str = ""
    title: str = ""
    imported_at: datetime | None = None

    @property
    def removable(self) -> bool:
        return self.state == "removable"

    @property
    def why_it_stays(self) -> str:
        return WHY_IT_STAYS.get(self.state, "")

    @property
    def name(self) -> str:
        """What to call it on screen: its title, or failing that its file."""
        if self.title:
            return self.title
        if self.path is not None:
            return self.path.name
        return str(self.record_id)


def _plain(path: Path) -> Path:
    """``path`` with any ``..`` resolved away, without touching the disk.

    Containment is decided on the text. A record whose path climbs out of the
    store through ``..`` must not pass for one inside it.
    """
    return Path(os.path.normpath(path))


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


def _state(adapter: Adapter, record_id: UUID, path: Path | None, roots: Sequence[Path]) -> State:
    if path is None:
        return "unknown"
    if not _inside(path, roots):
        return "elsewhere"
    untouched = provenance.untouched_since_import(adapter.name, record_id)
    if untouched is None:
        return "unknown"
    if not path.exists():
        return "gone"
    if untouched is False or adapter.used_since(path):
        return "changed"
    return "removable"


def survey(adapter: Adapter) -> list[Candidate]:
    """Every conversation Ferry converted into this tool, and which can go.

    Purely a question: nothing is changed by asking.
    """
    tool = adapter.name
    roots = [_plain(root) for root in adapter.written_roots()]
    found: list[Candidate] = []
    for record_id in provenance.recorded(tool):
        origin = provenance.recall(tool, record_id)
        written = provenance.written_file(tool, record_id)
        path = _plain(Path(written.path)) if written is not None else None
        found.append(
            Candidate(
                record_id=record_id,
                state=_state(adapter, record_id, path, roots),
                path=path,
                came_from=origin.original_tool if origin is not None else "",
                title=provenance.title_of(tool, record_id) or "",
                imported_at=origin.imported_at if origin is not None else None,
            )
        )
    return found


def remove(adapter: Adapter, options: RemoveOptions) -> Iterator[RemoveEvent]:
    """Delete what Ferry wrote into this tool, as far as it safely can.

    One conversation failing never stops the others: each failure is an
    ``error`` event, and whatever it left behind is finished by running this
    again.
    """
    wanted = [c for c in survey(adapter) if not options.only or str(c.record_id) in options.only]
    chosen = [c for c in wanted if c.removable]
    yield RemoveEvent(
        kind="started",
        message=f"{count_of(len(chosen), 'conversation')} to delete",
        total=len(chosen),
    )

    # Once per reason, not once per conversation.
    staying: dict[str, int] = {}
    for candidate in wanted:
        if not candidate.removable:
            staying[candidate.why_it_stays] = staying.get(candidate.why_it_stays, 0) + 1
    for why, count in staying.items():
        yield RemoveEvent(
            kind="note", message=f"{count_of(count, 'conversation')} left alone: {why}"
        )

    if not chosen:
        yield RemoveEvent(kind="done", message=f"nothing to delete from {adapter.display_name}")
        return

    verb = "would be deleted" if options.dry_run else "deleted"
    busy = adapter.in_use()
    if busy and not options.dry_run:
        yield RemoveEvent(kind="error", message=busy)
        yield RemoveEvent(kind="done", message=f"0 of {len(chosen)} {verb}")
        return
    if busy:
        yield RemoveEvent(kind="warning", message=busy)

    backed_up: set[Path] = set()
    deleted = 0
    for candidate in chosen:
        for event in _remove_one(adapter, candidate, options, backed_up):
            if event.kind == "progress":
                deleted += 1
            yield event
    yield RemoveEvent(kind="done", message=f"{deleted} of {len(chosen)} {verb}")


def _remove_one(
    adapter: Adapter, candidate: Candidate, options: RemoveOptions, backed_up: set[Path]
) -> Iterator[RemoveEvent]:
    tool = adapter.name
    cid = str(candidate.record_id)
    path = candidate.path
    if path is None:  # pragma: no cover - a removable candidate always has one
        return

    # Asked again at the moment of acting, not trusted from the list. The
    # person may have opened the conversation between choosing it and saying
    # yes, and that makes it theirs.
    if provenance.untouched_since_import(tool, candidate.record_id) is not True or (
        adapter.used_since(path)
    ):
        yield RemoveEvent(
            kind="warning",
            conversation_id=cid,
            message=f"{candidate.name}: changed since the list was made, so it was left alone",
        )
        return

    listing = adapter.listing(path)
    if options.dry_run:
        where = f" and its entry in {listing.name}" if listing is not None else ""
        yield RemoveEvent(
            kind="progress", conversation_id=cid, message=f"would delete {path.name}{where}"
        )
        return

    if options.backup:
        try:
            back_up(path, tool)
            # The list is shared by every conversation in it, so it is copied
            # the first time this run changes it rather than once per delete.
            if listing is not None and listing.is_file() and listing not in backed_up:
                back_up(listing, tool)
                backed_up.add(listing)
        except OSError as exc:
            yield RemoveEvent(
                kind="error",
                conversation_id=cid,
                message=(
                    f"{candidate.name}: could not copy it to your backups first, "
                    f"so nothing was deleted ({exc})"
                ),
            )
            return

    try:
        adapter.unlist(path)
    except RemovalBlocked as exc:
        yield RemoveEvent(
            kind="error",
            conversation_id=cid,
            message=f"{candidate.name}: {exc}. Nothing was deleted.",
        )
        return

    try:
        path.unlink()
    except OSError as exc:
        yield RemoveEvent(
            kind="error",
            conversation_id=cid,
            message=(
                f"{candidate.name}: taken out of the list, but the file could not be "
                f"deleted ({exc}). Run this again to finish."
            ),
        )
        return

    stuck: list[str] = []
    for extra in adapter.companions(path):
        try:
            extra.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            stuck.append(extra.name)
            continue
        _prune(extra.parent, stop=path.parent)

    # Last, so a delete interrupted before this point is still on the list of
    # things Ferry wrote, and the next run can finish it.
    provenance.forget(tool, candidate.record_id)
    if stuck:
        yield RemoveEvent(
            kind="warning",
            conversation_id=cid,
            message=f"{candidate.name}: deleted, but {', '.join(stuck)} could not be",
        )
    yield RemoveEvent(kind="progress", conversation_id=cid, message=f"deleted {candidate.name}")


def _prune(directory: Path, *, stop: Path) -> None:
    """Remove directories left empty, up to but never including ``stop``."""
    while directory != stop and directory.is_relative_to(stop):
        try:
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent
