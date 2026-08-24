"""Building synthetic Antigravity databases for the tests.

**Real schema, invented content.** The table definitions and field numbers are
copied from Antigravity 2.8.1 so the tests exercise the shapes the adapter will
actually meet; every string in them was written for this file and no part of
any real conversation appears here (PLAN.md section 6.6).

The protobuf is assembled by hand rather than by a library, which is the point:
if the encoder here and the decoder under test shared code, a round-trip test
would prove only that the code agrees with itself.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

__all__ = [
    "SCHEMA",
    "build_database",
    "message",
    "string_field",
    "varint_field",
]

SCHEMA = """
CREATE TABLE `steps` (
    `idx` integer, `step_type` integer NOT NULL DEFAULT 0, `status` integer NOT NULL DEFAULT 0,
    `has_subtrajectory` numeric NOT NULL DEFAULT false, `metadata` blob, `error_details` blob,
    `permissions` blob, `task_details` blob, `render_info` blob, `step_payload` blob,
    `step_format` integer NOT NULL DEFAULT 0, PRIMARY KEY (`idx`));
CREATE TABLE `trajectory_meta` (
    `trajectory_id` text, `cascade_id` text, `trajectory_type` integer, `source` integer,
    PRIMARY KEY (`trajectory_id`));
CREATE TABLE `trajectory_metadata_blob` (
    `id` text DEFAULT "main", `data` blob, PRIMARY KEY (`id`));
CREATE TABLE `gen_metadata` (
    `idx` integer, `data` blob, `size` integer NOT NULL DEFAULT 0, PRIMARY KEY (`idx`));
"""


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | 0x80 if value else byte)
        if not value:
            return bytes(out)


def varint_field(number: int, value: int) -> bytes:
    """One varint field, encoded."""
    return _varint(number << 3) + _varint(value)


def string_field(number: int, text: str) -> bytes:
    """One length-delimited field holding text."""
    payload = text.encode("utf-8")
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def message(number: int, body: bytes) -> bytes:
    """``body`` wrapped as a nested message at ``number``."""
    return _varint(number << 3 | 2) + _varint(len(body)) + body


def _nest(path: tuple[int, ...], text: str) -> bytes:
    """A chain of nested messages ending in ``text`` at ``path``."""
    encoded = string_field(path[-1], text)
    for number in reversed(path[:-1]):
        encoded = message(number, encoded)
    return encoded


def build_database(
    path: Path,
    steps: list[tuple[int, int, str]],
    *,
    project_id: str = "99999999-9999-4999-8999-999999999999",
    created: int = 1_785_000_000,
    parent: str | None = None,
) -> Path:
    """Write a conversation database holding ``(idx, step_type, text)`` steps.

    The text is placed at whichever field path :mod:`ferry.adapters.antigravity
    .schema` says that step type uses, so a change to that table is reflected
    here rather than silently making the fixtures meaningless.
    """
    from ferry.adapters.antigravity import schema

    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        for index, step_type, text in steps:
            fields = schema.TEXT_FIELDS.get(step_type)
            payload = _nest(fields[0], text) if fields else string_field(200, text)
            metadata = message(1, varint_field(1, created + index))
            connection.execute(
                "INSERT INTO steps (idx, step_type, metadata, step_payload) VALUES (?, ?, ?, ?)",
                (index, step_type, metadata, payload),
            )
        # Field 5 names the parent and appears only in a subagent; field 6
        # names the root, which is the conversation itself at the top.
        root = parent or path.stem
        metadata = (
            message(2, varint_field(1, created))
            + (string_field(5, parent) if parent else b"")
            + string_field(6, root)
            + string_field(18, project_id)
        )
        connection.execute(
            "INSERT INTO trajectory_metadata_blob (id, data) VALUES ('main', ?)",
            (metadata,),
        )
        connection.commit()
    finally:
        connection.close()
    return path
