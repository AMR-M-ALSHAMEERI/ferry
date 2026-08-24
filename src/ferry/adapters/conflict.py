"""What to do when the conversation being imported is already there.

``skip`` and ``overwrite`` mean what they say. ``rename`` did not.

Every one of the four tools identifies a conversation **by its id, and puts
that id in the filename**. So the obvious reading of "rename" -- write it
beside the existing file under a different name -- produces a file whose name
and contents disagree about which conversation it is. Claude Code did exactly
that: ``<uuid>-1.jsonl`` holding ``sessionId: <uuid>``, sharing its spilled
tool output with the conversation it was trying not to overwrite. Every real
Claude Code transcript has those two agreeing; this one would not, and nothing
would say so.

So rename means **import it as a new conversation**: a fresh id, carried
consistently into the filename, the records inside it, and anything keyed on
it. Both copies then exist and both are valid, which is what someone choosing
"keep both" is asking for.

A new id is deliberately random rather than derived from the old one. A
derived id would be the same on every run, so choosing "keep both" twice would
land on the copy it made the first time -- a rename that overwrites.

Antigravity is the exception and says so. Its conversations are restored from
the original database, and the id is written through protobuf blobs Ferry can
rewrite paths in but has no schema to re-identify. Refusing is the honest
answer; a half-re-identified database is not.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from ferry.ucs import Conversation

__all__ = ["RENAME_NOT_POSSIBLE", "rename_note", "reidentify"]

RENAME_NOT_POSSIBLE = (
    "already there, and it cannot be imported as a copy: the conversation's id "
    "is written through the stored database, which Ferry has no schema to change"
)
"""Why Antigravity refuses ``rename``. Phrased as what it means for the user,
not as an internal limitation."""


def reidentify(conversation: Conversation) -> UUID:
    """Give ``conversation`` a new id, in place, and return it.

    The caller must derive **every** path and record from the returned id
    afterwards -- filename, index entry, sidecar directory. An id changed in
    one place and not another is the failure this function exists to prevent.
    """
    conversation.id = uuid4()
    return conversation.id


def rename_note(original: UUID, replacement: UUID) -> str:
    """What to tell the user about a conversation that arrived under a new id.

    Both ids, because the old one is how they will look for it in the bundle
    and the new one is how they will find it in the tool.
    """
    return (
        f"already there, so this was imported as a separate copy: "
        f"{original} is now {replacement} in your history"
    )
