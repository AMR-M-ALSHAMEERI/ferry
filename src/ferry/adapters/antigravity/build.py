"""Building an Antigravity conversation from UCS, rather than restoring one.

Until this module, Antigravity was the one tool Ferry could read and not write.
The reason on file was that a conversation is restored from its original
database, which Ferry can copy but cannot invent -- and every part of that
turned out to be wrong when measured against Antigravity 2.8.1:

* a conversation is **7 tables** with a plain schema, ``user_version = 1``, and
  no migration bookkeeping to get wrong
* ``wire.py`` parsed **25 of 25** blobs in the conversation examined
* only two wire types appear, so the encoder it was missing is four functions
* ``schema.py`` already recorded where every step type keeps its text

A synthesised ``trajectory_metadata_blob`` came out **byte-identical to a real
one except for field 15** -- 352 to 380 bytes that do not parse as a message and
differ per conversation. It is left out rather than invented, and a conversation
without it opens and reads.

**What is written, and nothing more.** Two step types: ``USER_INPUT`` (14) for a
question and ``PLANNER_RESPONSE`` (15) for an answer. A conversation that
happened in another tool did not run Antigravity's tools, so its tool calls are
carried as text inside the answer -- the same promise every other target keeps,
and the reason `CODE_ACTION` steps are never written.

**The user's words go in twice.** A real ``USER_INPUT`` step carries the same
bytes at ``19.2`` and at ``19.3.1``. Writing only 19.2 produces a conversation
whose title bar holds the question and whose bubble is empty: one is what is
sent to the model, the other is what the interface draws. Copilot and Codex both
did this too, in their own vocabulary, and it looked like data loss all three
times.

Writing the database is only half of it. See :mod:`ferry.adapters.antigravity.index`
for the second store, without which none of this is listed.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID

from ferry.adapters.antigravity import wire
from ferry.core.continuable import as_text, call_line
from ferry.ucs import (
    Conversation,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

__all__ = [
    "BUILD_NOTES",
    "SCHEMA",
    "assistant_payload",
    "build_database",
    "model_identifier",
    "said_by",
    "step_metadata",
    "trajectory_blob",
    "user_payload",
]

BUILD_NOTES = (
    "written as an Antigravity conversation rather than restored from one: "
    "checkpoints, file snapshots and the agent's own working state are not carried",
    "tool calls are written as text, never as steps Antigravity is shown as having run",
)
"""What building a conversation costs, said in the record as well as on screen.

The provenance block is the copy that outlives the confirmation screen, so it
carries the assessment's notes and these -- the same rule Codex learned at
ledger #220 when it stored only half of them.
"""

USER_INPUT: Final = 14
PLANNER_RESPONSE: Final = 15

STATUS_DONE: Final = 3
"""``steps.status`` on every one of the 2,725 steps measured."""

TRAJECTORY_TYPE: Final = 4
SOURCE: Final = 1
"""``trajectory_meta``, identical in all six conversations on the probe machine."""

FIELD_10: Final = bytes.fromhex("8a01067a042a020a00")
"""``trajectory_metadata_blob`` field 10, byte-identical in both root conversations.

A message holding field 17, holding field 15, holding an empty field 1. Written
as the bytes it is rather than described as a shape nobody has decoded, because
naming its parts would claim an understanding that was never measured.
"""

SCHEMA: Final = (
    "CREATE TABLE `trajectory_meta` (`trajectory_id` text,`cascade_id` text,"
    "`trajectory_type` integer,`source` integer,PRIMARY KEY (`trajectory_id`))",
    "CREATE TABLE `steps` (`idx` integer,`step_type` integer NOT NULL DEFAULT 0,"
    "`status` integer NOT NULL DEFAULT 0,`has_subtrajectory` numeric NOT NULL DEFAULT false,"
    "`metadata` blob,`error_details` blob,`permissions` blob,`task_details` blob,"
    "`render_info` blob,`step_payload` blob,`step_format` integer NOT NULL DEFAULT 0,"
    "PRIMARY KEY (`idx`))",
    "CREATE INDEX `idx_steps_status` ON `steps`(`status`)",
    "CREATE INDEX `idx_steps_step_type` ON `steps`(`step_type`)",
    "CREATE TABLE `gen_metadata` (`idx` integer,`data` blob,`size` integer NOT NULL DEFAULT 0,"
    "PRIMARY KEY (`idx`))",
    "CREATE TABLE `executor_metadata` (`idx` integer,`data` blob,PRIMARY KEY (`idx`))",
    "CREATE TABLE `parent_references` (`idx` integer,`data` blob,PRIMARY KEY (`idx`))",
    'CREATE TABLE `trajectory_metadata_blob` (`id` text DEFAULT "main",`data` blob,'
    "PRIMARY KEY (`id`))",
    "CREATE TABLE `battle_mode_infos` (`idx` integer,`data` blob,PRIMARY KEY (`idx`))",
)
"""Antigravity's own schema, as ``sqlite_master`` reports it.

Copied because there is nothing here to get wrong: no migration table, no
version bookkeeping, and ``user_version = 1`` set alongside. The Codex state
database was the opposite case -- copying its schema left its migration table
empty and Codex refused to start -- so the difference is stated rather than
assumed.
"""


def _epoch(moment: datetime | None) -> int:
    return int((moment or datetime.now(UTC)).astimezone(UTC).timestamp())


def said_by(message: Message, source_tool: str) -> str:
    """One message as the text Antigravity will show.

    Thinking is dropped: its signature is issued by the vendor whose model
    produced it and cannot be reissued here. A tool call becomes the same
    readable line every other target gets, never a step Antigravity could be
    read as having run.
    """
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ToolUseBlock):
            parts.append(call_line(source_tool, block.name, block.input))
        elif isinstance(block, ToolResultBlock):
            # `output` is `Any`: a string from one tool, a list of content
            # blocks from another. Flattened through the one helper that
            # already knows every shape of it.
            output = as_text(block.output).strip()
            if output:
                parts.append(output)
    return "\n\n".join(p for p in parts if p.strip())


def step_metadata(when: int) -> bytes:
    """A step's time, at the field path every one of 2,725 real steps carries."""
    return wire.moment(1, when) + wire.number(3, 1)


def user_payload(said: str) -> bytes:
    """What the person typed, written at both 19.2 and 19.3.1. See the module docstring."""
    return (
        wire.number(1, USER_INPUT)
        + wire.number(4, STATUS_DONE)
        + wire.block(19, wire.string(2, said) + wire.block(3, wire.string(1, said)))
    )


def assistant_payload(said: str) -> bytes:
    """An answer, at 20.1 and again at 20.8, as all 406 real ones carry it."""
    return (
        wire.number(1, PLANNER_RESPONSE)
        + wire.number(4, STATUS_DONE)
        + wire.block(20, wire.string(1, said) + wire.string(8, said))
    )


def trajectory_blob(conversation_id: UUID, project_id: str, when: int, identifier: bytes) -> bytes:
    """The metadata a root conversation carries, minus the field nobody decoded.

    Field 5 is deliberately absent. A conversation that names a parent is a
    subagent, and Antigravity never lists those -- which cost this milestone two
    rounds, because the template first borrowed from was one.
    """
    return (
        wire.block(1, wire.block(1, identifier) + wire.block(3, b""))
        + wire.moment(2, when)
        + wire.string(3, str(conversation_id))
        + wire.string(6, str(conversation_id))
        + wire.block(7, identifier)
        + wire.block(10, FIELD_10)
        + wire.string(18, project_id)
    )


def model_identifier(database: Path) -> bytes:
    """The one value Ferry copies rather than derives, read from a conversation.

    ``trajectory_metadata_blob`` field 7 is 39 to 44 characters of eight
    ``/``-separated segments with no digits, and it does not contain the
    conversation id -- a model or resource name belonging to Antigravity, not to
    the person. It is copied because inventing a model identifier would be
    asserting which model produced work that happened in another tool.

    Returns empty bytes when there is nothing to read, which leaves the field
    out entirely rather than guessing at one.
    """
    from ferry.adapters.antigravity import paths

    try:
        with paths.open_readonly(database) as connection:
            row = connection.execute("SELECT data FROM trajectory_metadata_blob").fetchone()
    except (sqlite3.Error, OSError):
        return b""
    if row is None or not isinstance(row[0], bytes):
        return b""
    for field in wire.parse(row[0]) or []:
        if field.number == 7:
            return field.value
    return b""


def build_database(
    path: Path,
    conversation: Conversation,
    *,
    project_id: str,
    identifier: bytes,
) -> int:
    """Write one conversation as an Antigravity database. Returns the step count.

    Raises:
        sqlite3.Error: If the database cannot be written.
    """
    created = _epoch(conversation.created_at)
    connection = sqlite3.connect(path)
    try:
        with connection:
            for statement in SCHEMA:
                connection.execute(statement)
            connection.execute("PRAGMA user_version = 1")
            connection.execute(
                "INSERT INTO trajectory_meta VALUES (?,?,?,?)",
                (str(conversation.id), str(conversation.id), TRAJECTORY_TYPE, SOURCE),
            )
            connection.execute(
                "INSERT INTO trajectory_metadata_blob VALUES (?,?)",
                ("main", trajectory_blob(conversation.id, project_id, created, identifier)),
            )

            idx = 0
            for message in conversation.messages:
                said = said_by(message, conversation.source_tool)
                if not said:
                    continue
                when = _epoch(message.timestamp or conversation.created_at)
                if message.role == "user":
                    step_type, payload = USER_INPUT, user_payload(said)
                else:
                    # Everything that is not the person is written as the
                    # assistant speaking. A system note read as a question would
                    # put words in their mouth, which is the one direction this
                    # must not get wrong.
                    step_type, payload = PLANNER_RESPONSE, assistant_payload(said)
                connection.execute(
                    "INSERT INTO steps (idx, step_type, status, has_subtrajectory,"
                    " metadata, step_payload, step_format) VALUES (?,?,?,?,?,?,?)",
                    (idx, step_type, STATUS_DONE, 0, step_metadata(when), payload, 0),
                )
                idx += 1
    finally:
        connection.close()
    return idx
