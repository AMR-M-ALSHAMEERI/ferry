"""Whether a database Ferry built has only been opened, not used.

Antigravity keeps each conversation in SQLite, and **SQLite rewrites part of a
database's header just by opening it.** Measured on the human's real store: a
conversation Ferry built, opened once in Antigravity to check it had arrived,
no longer matched the checksum Ferry recorded -- and rebuilding the original
from its bundle showed exactly six bytes had moved, all in the first hundred:

====== =========================================================
18-19  the journal mode, switched from rollback (1) to WAL (2)
27     the change counter, 11 to 12
95     the "version valid for" number, which follows the counter
98-99  the SQLite version stamp, Ferry's Python's to Antigravity's
====== =========================================================

Every one of the 349 steps, and every other page of the file, was identical.
Treating that as "you worked in this" made the delete refuse the very
conversation someone had imported, looked at, and wanted gone.

So when the checksum no longer matches, the header fields SQLite rewrites are
put back to the values Ferry's build could have left, and the checksum is tried
again. **A match proves every other byte is what Ferry wrote**, so it is a proof
and not a guess -- and any real use, a single step added, fails it. Records
written before this existed need nothing new: the recorded checksum is all it
compares against.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Final

from ferry.core.provenance import Written

__all__ = ["OPENS_TRIED", "opened_not_changed"]

SQLITE_HEADER: Final = b"SQLite format 3\x00"

ROLLBACK: Final = b"\x01\x01"
"""Bytes 18-19 for a database in rollback-journal mode, which is how Ferry builds one."""

OPENS_TRIED: Final = 16
"""How far back the change counter is searched.

Switching to WAL moves it by one, and a database in WAL mode does not move it
again while it is read. Sixteen is generous for opening and looking, and small
enough that the search stays cheap on a large conversation.
"""


def _version_number(parts: tuple[int, int, int]) -> bytes:
    major, minor, patch = parts
    return (major * 1_000_000 + minor * 1_000 + patch).to_bytes(4, "big")


def opened_not_changed(path: Path, was: Written) -> bool:
    """Whether ``path`` differs from what Ferry wrote only where opening it writes.

    ``False`` whenever it cannot prove otherwise: a different size, a file that
    is not SQLite, or no arrangement of those header fields reproducing the
    recorded checksum.
    """
    try:
        data = bytearray(path.read_bytes())
    except OSError:
        return False
    if len(data) != was.bytes or len(data) < 100 or not data.startswith(SQLITE_HEADER):
        return False

    counter = int.from_bytes(data[24:28], "big")
    modes = {bytes(data[18:20]), ROLLBACK}
    versions = {bytes(data[96:100]), _version_number(sqlite3.sqlite_version_info)}
    for mode in modes:
        for version in versions:
            for count in range(counter, max(-1, counter - OPENS_TRIED), -1):
                stamp = count.to_bytes(4, "big")
                data[18:20] = mode
                data[24:28] = stamp
                data[92:96] = stamp
                data[96:100] = version
                if hashlib.sha256(data).hexdigest() == was.sha256:
                    return True
    return False
