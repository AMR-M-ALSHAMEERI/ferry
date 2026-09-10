"""The entry that makes a written conversation exist, in the store nobody had noticed.

A conversation database can be **byte-for-byte identical** to one Antigravity
lists, with only its ids changed, and Antigravity will not show it. That was
measured, and it is what ended four rounds of guessing at the format: the
``conversations/`` folder is not what the list is built from.

The language server says so in a line that had been sitting in its log
unread::

    Creating trajectory store manager with proto store and SQLite store

Two stores. The SQLite store is ``conversations/<uuid>.db``. The proto store is
``~/.gemini/antigravity/agyhub_summaries_proto.pb`` -- a repeated field of
entries, one per conversation, root and subagent alike.

**The fifth gate after the write.** A Copilot transcript needs a chat index
entry, a Claude Code conversation needs a trusted folder, a Codex rollout needs
a ``threads`` row and needs ``cwd`` spelled its way, and this needs an entry
here. Every target has had one, and this one hid longest because a folder full
of self-describing databases looks exactly like a list.

An entry is ``{1: <conversation id>, 2: <summary>}``, and every part of the
summary was derived by measuring the six real entries rather than guessed:

====  ======================================================================
1     the title
2     **the step count** -- 2161, 9, 456, 14, 67, 18, matching the databases
      exactly
3     when it was last updated
4     a cascade id, distinct from the conversation id
5     ``1`` in all six
7     when it was created
9     the model identifier, in the same wrapper the trajectory blob uses
10    created again
15    a flag, empty on half the conversations measured, so written empty
16    a count that is ``0`` on every conversation under a hundred steps
17    the trajectory metadata, minus the two fields it does not carry
22    ``4`` in all six
====  ======================================================================

Field 23 is **not** written. It is ``0`` where it appears and absent from two of
the six -- including the entry that was copied to prove a conversation could be
listed at all. The shape proven to work is the one without it, and a field added
because it appears elsewhere is a guess wearing a measurement's clothes.

**Antigravity must not be running.** It holds this file in memory and writes it
back on exit, so an entry added underneath a running app is discarded -- the
same failure as writing a Codex row while Codex is open, and reported the same
way rather than being silently lost.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid5

from ferry.adapters.antigravity import paths, wire
from ferry.adapters.antigravity.build import trajectory_blob

__all__ = [
    "INDEX_NAME",
    "AntigravityIndexLocked",
    "drop_entry",
    "entry_for",
    "index_path",
    "upsert_entry",
]

INDEX_NAME: Final = "agyhub_summaries_proto.pb"

_ENTRY: Final = 1
"""The repeated field holding one conversation each."""


class AntigravityIndexLocked(RuntimeError):
    """The index could not be written, and saying otherwise would be a lie.

    Raised rather than worked around. Antigravity rewrites this file from memory
    when it exits, so an entry written while it is running is discarded -- and a
    cheerful "imported" over a conversation that will never be listed is the
    failure this milestone exists to prevent.
    """


def index_path(env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    return paths.data_dir(env) / INDEX_NAME


def _cascade_id(conversation_id: UUID) -> str:
    """An id of its own, derived so a re-import produces the same one.

    Not the conversation id: the six real entries keep them apart, and giving
    two conversations one cascade would be inventing a relationship between
    them.
    """
    return str(uuid5(NAMESPACE_URL, f"ferry-antigravity-cascade:{conversation_id}"))


def entry_for(
    conversation_id: UUID,
    *,
    title: str,
    project_id: str,
    identifier: bytes,
    steps: int,
    created: int,
    updated: int,
) -> bytes:
    """One index entry, built from the table in the module docstring."""
    metadata = trajectory_blob(conversation_id, project_id, created, identifier)
    carried = {f.number: f.value for f in (wire.parse(metadata) or [])}
    summary_metadata = b"".join(
        wire.block(number, carried[number]) for number in (1, 2, 3, 6, 7, 18) if number in carried
    )

    summary = (
        wire.string(1, title)
        + wire.number(2, steps)
        + wire.moment(3, updated)
        + wire.string(4, _cascade_id(conversation_id))
        + wire.number(5, 1)
        + wire.moment(7, created)
        + wire.block(9, wire.block(1, identifier) + wire.block(3, b""))
        + wire.moment(10, created)
        + wire.block(15, b"")
        + wire.number(16, 0)
        + wire.block(17, summary_metadata)
        + wire.number(22, 4)
    )
    return wire.block(_ENTRY, wire.string(1, str(conversation_id)) + wire.block(2, summary))


def upsert_entry(path: Path, conversation_id: UUID, entry: bytes, *, running: bool = False) -> None:
    """Add or replace one conversation's entry, leaving every other one exactly as it was.

    Rewritten field by field from the parsed original, so an entry Ferry did not
    write is put back as the bytes it arrived as. The file is replaced atomically:
    this is a list of somebody's conversations, and a half-written one loses all
    of them rather than one.

    Raises:
        AntigravityIndexLocked: If Antigravity is running, the index is missing,
            or it cannot be read or replaced.
    """
    if running:
        raise AntigravityIndexLocked(
            "Antigravity is running - it rewrites its conversation list on exit, "
            "so close it and import again"
        )
    if not path.is_file():
        raise AntigravityIndexLocked(
            f"no conversation index at {path}; this is not an Antigravity "
            "installation Ferry recognises"
        )
    kept, _ = _without(path, conversation_id)
    _replace(path, b"".join(kept) + entry)


def drop_entry(path: Path, conversation_id: UUID, *, running: bool = False) -> bool:
    """Take one conversation out of the list, leaving every other entry exactly as it was.

    The undo of :func:`upsert_entry`, held to the same care: every other entry
    is put back as the bytes it arrived as, and the file is replaced whole.
    Returns whether there was an entry to take out.

    A missing index lists nothing, which is the state being asked for, so it is
    not an error. A file with nothing to take out is left untouched rather than
    rewritten into the same bytes.

    Raises:
        AntigravityIndexLocked: If Antigravity is running, or the index cannot
            be read or replaced.
    """
    if running:
        raise AntigravityIndexLocked(
            "Antigravity is running - it rewrites its conversation list on exit, "
            "so close it and try again"
        )
    if not path.is_file():
        return False
    kept, dropped = _without(path, conversation_id)
    if dropped:
        _replace(path, b"".join(kept))
    return dropped


def _without(path: Path, conversation_id: UUID) -> tuple[list[bytes], bool]:
    """Every field of the index but this conversation's entry, as encoded, and
    whether it had one."""
    try:
        original = path.read_bytes()
    except OSError as exc:
        raise AntigravityIndexLocked(f"cannot read {path.name}: {exc}") from exc

    fields = wire.parse(original)
    if fields is None:
        raise AntigravityIndexLocked(f"{path.name} is not in the format Ferry measured")

    wanted = str(conversation_id)
    kept: list[bytes] = []
    dropped = False
    for field in fields:
        if field.number == _ENTRY:
            inner = {f.number: f.value for f in (wire.parse(field.value) or [])}
            if inner.get(1, b"").decode("utf-8", "replace") == wanted:
                dropped = True
                continue
        kept.append(field.encoded)
    return kept, dropped


def _replace(path: Path, payload: bytes) -> None:
    """Write the index whole. A half-written one loses every conversation in it."""
    try:
        handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
        with os.fdopen(handle, "wb") as out:
            out.write(payload)
        os.replace(temporary, path)
    except OSError as exc:
        raise AntigravityIndexLocked(f"could not write {path.name}: {exc}") from exc
