"""Where a converted conversation's origin is recorded, and why it is here.

Ferry stamps every cross-tool write with a :class:`~ferry.ucs.Provenance`
block: where the conversation came from, what it was written into, which
version did it, and everything the conversion gave up. That stamp used to be
built during an import and then dropped on the floor -- assigned to an object
that went out of scope, serialised nowhere, read by nothing. **Three documents
promised it and the file never carried it**, so a converted conversation was
permanently indistinguishable from one that had really happened in the target
tool. Found by rehearsing acceptance item A7b.8; ledger #209.

**Why a file of Ferry's own, and not a field in the target's transcript.**
Writing the stamp into the tool's own file is the version that travels: copy
the transcript anywhere and its origin goes with it. It was rejected for one
reason that no amount of testing could have made safe -- *the tool owns that
file*. Claude Code rewrites a transcript when the conversation is resumed, and
a field it does not recognise is very unlikely to survive that rewrite. The
stamp would then be present right up until the first time the conversation was
used, and absent afterwards, with nothing to mark its going. **A record that
disappears when the conversation is used is worse than one that was never
promised**, because the check that proves it works is the check made before it
matters.

So the record lives in ``~/.ferry/provenance/<tool>/<conversation id>.json``,
which Ferry owns outright: nothing else writes there, nothing else prunes it,
and no other program's release can change what it means.

**What this does not do**, said plainly rather than discovered later: the
record is keyed by conversation id on *this machine*. Copy a transcript to
another machine by hand and the stamp does not follow it. Exporting through
Ferry does carry it, because the export reads it back.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

from ferry.ucs import Provenance

__all__ = ["ROOT_ENV", "forget", "recall", "record", "root"]

ROOT_ENV = "FERRY_PROVENANCE_DIR"
"""Redirects the store, so a test never writes into the developer's own.

Honoured with no fallback: if it is set, it is used. A helper that quietly
reverted to the real home directory when the override looked wrong would put
test data in the one place this project promises never to touch.
"""


def root(env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """The directory holding every provenance record.

    **Resolved from the process environment, not from an adapter's.** Each
    adapter carries an ``env`` describing where *its tool* keeps things, and
    the first version of this passed that straight through. It reads sensibly
    and it is wrong: a redirected tool store says nothing about where Ferry's
    own files belong, so a caller redirecting one tool silently sent Ferry's
    records to the real home directory. The whole test suite did exactly that
    and left thirteen records in the developer's own ``~/.ferry``.

    ``env`` stays for callers who mean to name the directory outright, such as
    a test.
    """
    environ = os.environ if env is None else env
    override = environ.get(ROOT_ENV)
    if override:
        return Path(override)
    return Path.home() / ".ferry" / "provenance"


def _path(
    tool: str, conversation_id: UUID, env: os._Environ[str] | dict[str, str] | None = None
) -> Path:
    return root(env) / tool / f"{conversation_id}.json"


def record(
    tool: str,
    conversation_id: UUID,
    provenance: Provenance,
    env: os._Environ[str] | dict[str, str] | None = None,
) -> Path:
    """Write the stamp for one converted conversation.

    Written through a temporary file and a rename, like everything else Ferry
    puts on disk: a half-written provenance record read back later would say
    something false about where a conversation came from, and being confidently
    wrong about that is worse than having no record at all.
    """
    destination = _path(tool, conversation_id, env)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".json.ferry-tmp")
    temporary.write_text(provenance.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(destination)
    return destination


def recall(
    tool: str, conversation_id: UUID, env: os._Environ[str] | dict[str, str] | None = None
) -> Provenance | None:
    """The stamp for one conversation, or ``None`` if it was never converted.

    A missing record is the ordinary case -- most conversations are native and
    have nothing to declare -- so it is not an error. **An unreadable record is
    also treated as absent**: a corrupt file means Ferry cannot say where the
    conversation came from, and refusing to export the conversation at all
    would punish the user for damage to a note about it.
    """
    path = _path(tool, conversation_id, env)
    if not path.is_file():
        return None
    try:
        return Provenance.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def forget(
    tool: str, conversation_id: UUID, env: os._Environ[str] | dict[str, str] | None = None
) -> bool:
    """Drop the stamp for one conversation. Used when its conversation is
    deleted, so the store does not accumulate records of things that are gone."""
    path = _path(tool, conversation_id, env)
    if not path.is_file():
        return False
    path.unlink()
    return True
