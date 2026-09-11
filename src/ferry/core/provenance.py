"""Where a converted conversation's origin is recorded, and why it is here.

Ferry stamps every cross-tool write with a :class:`~ferry.ucs.Provenance`
block: where the conversation came from, what it was written into, which
version did it, and everything the conversion gave up. That stamp used to be
built during an import and then dropped on the floor -- assigned to an object
that went out of scope, serialised nowhere, read by nothing. **Three documents
promised it and the file never carried it**, so a converted conversation was
permanently indistinguishable from one that had really happened in the target
tool. Found by rehearsing the acceptance checklist by hand.

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

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from ferry.ucs import Provenance

__all__ = [
    "ROOT_ENV",
    "Written",
    "fingerprint",
    "forget",
    "recall",
    "record",
    "recorded",
    "root",
    "title_of",
    "untouched_since_import",
    "written_file",
]

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


@dataclass(frozen=True)
class Written:
    """The file Ferry wrote, and what it looked like when Ferry left it.

    **Kept for a feature that does not exist yet, and that is the point.** A
    later "remove the conversations Ferry put here" needs to answer a question
    the provenance stamp alone cannot: *has the person worked in this since?* A
    migrated conversation that was then continued holds real work, and deleting
    it because Ferry once created it would destroy exactly what the tool is for.

    The fingerprint answers it. Unchanged since the import means Ferry's own
    output and nothing else; changed or missing means the tool or the person has
    been there, and a delete has to say so instead of proceeding.

    It is recorded now rather than when that feature is built, because it cannot
    be recovered afterwards: every conversation migrated before the field
    existed would be one a delete could never safely offer.
    """

    path: str
    sha256: str
    bytes: int

    content: str | None = None
    """A second checksum, over what survives the tool opening the file.

    Only for a tool that rewrites a file just by showing it, so thoroughly that
    no checksum of the bytes can survive: Codex re-files an older rollout in its
    current format on opening. What the adapter chooses to cover is what it
    measured the rewrite keeping. ``None`` everywhere else, and in every record
    written before this existed.
    """

    def as_json(self) -> dict[str, str | int]:
        document: dict[str, str | int] = {
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }
        if self.content is not None:
            document["content"] = self.content
        return document


def fingerprint(path: Path) -> Written | None:
    """What ``path`` holds right now, or ``None`` if it cannot be read."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return Written(path=str(path), sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))


def record(
    tool: str,
    conversation_id: UUID,
    provenance: Provenance,
    env: os._Environ[str] | dict[str, str] | None = None,
    written: Written | None = None,
    title: str | None = None,
) -> Path:
    """Write the stamp for one converted conversation.

    ``title`` is kept so that a list of what Ferry wrote can be read by a
    person. Without it the delete screen could only offer file names, and a
    column of UUIDs is not something anyone can safely choose from.

    Written through a temporary file and a rename, like everything else Ferry
    puts on disk: a half-written provenance record read back later would say
    something false about where a conversation came from, and being confidently
    wrong about that is worse than having no record at all.

    The file holds more than the UCS ``Provenance`` block, deliberately. This is
    Ferry's own record, not part of a bundle, so ``written`` can be kept here
    without touching the conversation schema -- which would mean a UCS version
    bump and every bundle already exported becoming unreadable, for a field that
    describes an operation on this machine rather than anything about the
    conversation.
    """
    destination = _path(tool, conversation_id, env)
    destination.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {"provenance": provenance.model_dump(mode="json")}
    if written is not None:
        document["written"] = written.as_json()
    if title:
        document["title"] = title
    temporary = destination.with_suffix(".json.ferry-tmp")
    temporary.write_text(json.dumps(document, indent=2), encoding="utf-8")
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
    document = _read(tool, conversation_id, env)
    if document is None:
        return None
    try:
        return Provenance.model_validate(document["provenance"])
    except (KeyError, ValueError):
        return None


def _read(
    tool: str, conversation_id: UUID, env: os._Environ[str] | dict[str, str] | None = None
) -> dict[str, Any] | None:
    path = _path(tool, conversation_id, env)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def written_file(
    tool: str, conversation_id: UUID, env: os._Environ[str] | dict[str, str] | None = None
) -> Written | None:
    """What Ferry wrote for this conversation, as it left it.

    ``None`` when there is no record, or when the record predates this being
    kept. **A caller must treat "no record" as "cannot tell", never as "safe"**
    -- the whole value of the fingerprint is refusing to act when it is absent.
    """
    document = _read(tool, conversation_id, env)
    if document is None:
        return None
    found = document.get("written")
    if not isinstance(found, dict):
        return None
    try:
        content = found.get("content")
        return Written(
            path=str(found["path"]),
            sha256=str(found["sha256"]),
            bytes=int(found["bytes"]),
            content=content if isinstance(content, str) else None,
        )
    except (KeyError, TypeError, ValueError):
        return None


def recorded(tool: str, env: os._Environ[str] | dict[str, str] | None = None) -> list[UUID]:
    """Every conversation with a record for ``tool``, in a stable order.

    A file whose name is not a conversation id is not a record -- a leftover
    from an interrupted write, say -- and is left out rather than guessed at.
    """
    directory = root(env) / tool
    if not directory.is_dir():
        return []
    found: list[UUID] = []
    for path in sorted(directory.glob("*.json")):
        try:
            found.append(UUID(path.stem))
        except ValueError:
            continue
    return found


def title_of(
    tool: str, conversation_id: UUID, env: os._Environ[str] | dict[str, str] | None = None
) -> str | None:
    """The title recorded with a conversation, or ``None`` if none was.

    Records written before titles were kept have none, and a caller shows the
    file name instead.
    """
    document = _read(tool, conversation_id, env)
    if document is None:
        return None
    title = document.get("title")
    return title if isinstance(title, str) and title else None


def untouched_since_import(
    tool: str, conversation_id: UUID, env: os._Environ[str] | dict[str, str] | None = None
) -> bool | None:
    """Whether the file still holds exactly what Ferry wrote.

    ``True`` unchanged, ``False`` changed or gone, and **``None`` meaning
    cannot tell** -- no record, or one written before fingerprints were kept.
    Three states rather than two on purpose: a delete offered on a guess is the
    failure this is here to prevent, and "I do not know" has to be sayable.
    """
    was = written_file(tool, conversation_id, env)
    if was is None:
        return None
    now = fingerprint(Path(was.path))
    if now is None:
        return False
    return now.sha256 == was.sha256


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
