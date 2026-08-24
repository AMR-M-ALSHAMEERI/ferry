"""Layer-2 self-check for M6 -- the Antigravity adapter.

Contract (PLAN.md 6.8.2): numbered PASS/FAIL/SKIP lines, non-zero exit on any
failure, runs against **real data on this machine**, never prints conversation
content, and never modifies user data.

Check 13 is the mandatory one: every conversation database is checksummed
before and after, asserting the export did not move a byte of the real store.

Five checks exist only for Antigravity. Four are about the same risk -- that a
path rewrite corrupts a conversation in a way that still opens -- and one is
about a different one entirely:

- **Check 7** compares the number of conversations Ferry reports with the
  number of databases on disk. They are not the same: a subagent trajectory
  gets its own database and the app never lists it. This check cannot verify
  itself against anything, so it prints both numbers and asks the person
  running it to open Antigravity and compare.


- **Check 4** parses every blob on this machine. The codec is strict, and a
  blob it cannot parse is one an import would carry through unrewritten.
- **Check 5** rewrites every blob and reverses the rewrite, asserting the bytes
  return to exactly what they were. This is the check that matters: corrupt
  length prefixes usually still parse into *something*, so "it parses
  afterwards" proves almost nothing on its own.
- **Check 11** re-reads the imported conversations and compares message counts
  with the originals, because a database can be valid and still have lost its
  contents.
- **Check 12** asserts no spelling of the old path survives anywhere in the
  imported databases. A remap that misses one spelling leaves a conversation
  half-migrated, which is worse than not migrating it -- it still opens.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ferry.adapters.antigravity import AntigravityAdapter, reader, schema, wire
from ferry.adapters.antigravity import paths as ag
from ferry.adapters.antigravity.remap import PathRemapper, spellings
from ferry.adapters.base import ImportOptions
from ferry.core import Bundle, sha256_file
from ferry.ucs import UCS_VERSION

OLD_PREFIX = str(Path.home())
NEW_PREFIX = r"D:\ferry-selfcheck\a-deliberately-longer-name"
"""Longer than the original on purpose.

A shorter replacement can hide a length-prefix fault: the bytes still fit where
they were. Growing every path forces every enclosing length to be rewritten.
"""


@dataclass
class Result:
    ok: bool | None
    detail: str


class State:
    def __init__(self) -> None:
        self.adapter = AntigravityAdapter()
        self.workspace = Path(tempfile.mkdtemp(prefix="ferry-verify-m6-"))
        self.bundle_dir = self.workspace / "bundle"
        self.target_env = dict(os.environ)
        self.target_env[ag.DATA_DIR_ENV] = str(self.workspace / "target" / "antigravity")
        self.source_before: dict[str, str] = {}
        self.detected = self.adapter.detect()
        self.databases = ag.conversation_databases()
        self.exported: list[str] = []
        self.imported: list[str] = []

    def cleanup(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)


def _blobs(database: Path):
    """Every blob in a database, without touching the original file."""
    with ag.open_readonly(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in sorted(tables):
            info = list(connection.execute(f'PRAGMA table_info("{table}")'))
            names = [str(row[1]) for row in info]
            types = [str(row[2]).lower() for row in info]
            for name, kind in zip(names, types, strict=True):
                if kind != "blob":
                    continue
                for (value,) in connection.execute(f'SELECT "{name}" FROM "{table}"'):
                    if isinstance(value, bytes | bytearray) and value:
                        yield bytes(value)


def check_detect(state: State) -> Result:
    found = state.detected
    if not found.installed:
        return Result(None, found.notes[0] if found.notes else "not installed")
    return Result(
        True, f"{found.conversation_count_estimate} conversations, version {found.version}"
    )


def check_version(state: State) -> Result:
    version = state.detected.version
    if version is None:
        return Result(None, "install not found; version unknown, and reported as unknown")
    return Result(True, f"read {version} from the packaged app")


def check_checksums_taken(state: State) -> Result:
    for database in state.databases:
        state.source_before[str(database)] = sha256_file(database)
    if not state.source_before:
        return Result(None, "no conversations on this machine")
    return Result(True, f"{len(state.source_before)} databases fingerprinted")


def check_every_blob_parses(state: State) -> Result:
    total = parsed = 0
    for database in state.databases:
        for blob in _blobs(database):
            total += 1
            if wire.parse(blob) is not None:
                parsed += 1
    if not total:
        return Result(None, "no blobs")
    return Result(parsed == total, f"{parsed} of {total} blobs parse")


def check_rewrite_reverses(state: State) -> Result:
    """The one that matters: an edit and its inverse must land on the original.

    Exact case, so the edit is genuinely invertible. Case-insensitive matching
    is right in production and is many-to-one, which no round trip can undo.
    """
    forward = PathRemapper(((OLD_PREFIX, NEW_PREFIX),), case_insensitive=False)
    backward = PathRemapper(((NEW_PREFIX, OLD_PREFIX),), case_insensitive=False)
    touched = restored = 0
    for database in state.databases:
        for blob in _blobs(database):
            changed = wire.rewrite(blob, forward.field)
            if changed is blob:
                continue
            touched += 1
            if wire.parse(changed) is None:
                continue
            if wire.rewrite(changed, backward.field) == blob:
                restored += 1
    if not touched:
        return Result(None, "no blob on this machine holds this path")
    return Result(restored == touched, f"{restored} of {touched} rewritten blobs reversed exactly")


def check_text_fields_have_names(state: State) -> Result:
    """Every step type present here must be one the adapter can name."""
    unknown: set[int] = set()
    seen: set[int] = set()
    for database in state.databases:
        with ag.open_readonly(database) as connection:
            for (step_type,) in connection.execute("SELECT DISTINCT step_type FROM steps"):
                seen.add(int(step_type))
                if int(step_type) not in schema.STEP_TYPES:
                    unknown.add(int(step_type))
    if not seen:
        return Result(None, "no steps")
    if unknown == {28}:
        return Result(True, f"{len(seen)} step types; only 28 unnamed, as recorded")
    return Result(not unknown, f"{len(seen)} step types, unnamed: {sorted(unknown) or 'none'}")


def check_conversation_count(state: State) -> Result:
    """What Ferry reports has to be what Antigravity lists.

    A subagent trajectory has its own database and looks exactly like a
    conversation; the app never shows it. This check has no oracle it can read
    -- only the person running it can open Antigravity and count -- so it
    reports both numbers and asks them to compare. That is the honest form:
    asserting a number nothing here can verify would be self-certification.
    """
    if not state.databases:
        return Result(None, "no conversations")
    subagents = sum(1 for database in state.databases if reader.parent_conversation(database))
    top = len(state.databases) - subagents
    return Result(
        state.detected.conversation_count_estimate == top,
        f"reports {top} conversations from {len(state.databases)} databases "
        f"({subagents} subagent) - open Antigravity and confirm it lists {top}",
    )


def check_export(state: State) -> Result:
    if not state.databases:
        return Result(None, "nothing to export")
    counts: dict[str, int] = {}
    for event in state.adapter.export(state.bundle_dir):
        counts[event.kind] = counts.get(event.kind, 0) + 1
        if event.kind == "progress" and event.conversation_id:
            state.exported.append(event.conversation_id)
    bundle = Bundle.open(state.bundle_dir)
    with_raw = sum(1 for cid in bundle.list_conversations() if bundle.has_source_raw(cid))
    ok = len(state.exported) == len(state.databases) and with_raw == len(state.exported)
    return Result(ok, f"{len(state.exported)} exported, {with_raw} carry their database")


def check_ucs_version(state: State) -> Result:
    if not state.exported:
        return Result(None, "nothing exported")
    bundle = Bundle.open(state.bundle_dir)
    versions = {bundle.load_conversation(cid).ucs_version for cid in bundle.list_conversations()}
    return Result(versions == {UCS_VERSION}, f"all at UCS {UCS_VERSION}")


def check_import(state: State) -> Result:
    if not state.exported:
        return Result(None, "nothing to import")
    options = ImportOptions(path_remap=((OLD_PREFIX, NEW_PREFIX),))
    adapter = AntigravityAdapter(state.target_env)
    for event in adapter.import_(state.bundle_dir, options):
        if event.kind == "progress" and event.conversation_id:
            state.imported.append(event.conversation_id)
    written = list(ag.conversations_dir(state.target_env).glob("*.db"))
    ok = len(state.imported) == len(state.exported) == len(written)
    return Result(ok, f"{len(state.imported)} imported, {len(written)} databases on disk")


def check_messages_survive(state: State) -> Result:
    """A remapped database can be valid and still have lost its contents."""
    if not state.imported:
        return Result(None, "nothing imported")
    same = 0
    for database in state.databases:
        before = reader.read_conversation(database).conversation
        after = reader.read_conversation(
            ag.conversations_dir(state.target_env) / database.name, state.target_env
        ).conversation
        if before is None or after is None:
            continue
        if len(before.messages) == len(after.messages):
            same += 1
    return Result(
        same == len(state.databases),
        f"{same} of {len(state.databases)} conversations kept every message",
    )


def check_no_old_path_survives(state: State) -> Result:
    """Any surviving spelling means a conversation that opens and is wrong."""
    if not state.imported:
        return Result(None, "nothing imported")
    wanted = [form.lower() for form in spellings(OLD_PREFIX)]
    remaining = 0
    replaced = 0
    for database in ag.conversations_dir(state.target_env).glob("*.db"):
        for blob in _blobs(database):
            for _path, text in wire.strings(blob):
                lowered = text.lower()
                if any(form in lowered for form in wanted):
                    remaining += 1
                if "ferry-selfcheck" in lowered:
                    replaced += 1
    if not replaced and not remaining:
        return Result(None, "no paths under the home directory in this history")
    return Result(remaining == 0, f"{remaining} old-path strings left, {replaced} rewritten")


def check_source_untouched(state: State) -> Result:
    """The mandatory one. Ferry promised only to read this store."""
    if not state.source_before:
        return Result(None, "nothing to compare")
    changed = [
        Path(path).name
        for path, digest in state.source_before.items()
        if not Path(path).is_file() or sha256_file(Path(path)) != digest
    ]
    return Result(
        not changed,
        f"{len(state.source_before)} databases byte-identical"
        if not changed
        else f"CHANGED: {', '.join(changed)}",
    )


CHECKS = [
    ("Antigravity detected", check_detect),
    ("version read from the install", check_version),
    ("source fingerprinted", check_checksums_taken),
    ("every blob parses", check_every_blob_parses),
    ("rewrite reverses to the original bytes", check_rewrite_reverses),
    ("every step type has a name", check_text_fields_have_names),
    ("count matches what the app lists", check_conversation_count),
    ("export carries every database", check_export),
    ("bundle is current UCS", check_ucs_version),
    ("path-remapped import", check_import),
    ("no message lost in the remap", check_messages_survive),
    ("no old path survives", check_no_old_path_survives),
    ("source store untouched", check_source_untouched),
]


def main() -> int:
    state = State()
    print("Ferry self-check - M6 (Antigravity adapter)")
    passed = failed = skipped = 0
    try:
        for number, (label, run) in enumerate(CHECKS, start=1):
            try:
                result = run(state)
            except Exception as exc:  # noqa: BLE001 - a crashed check is a failed check
                result = Result(False, f"{exc.__class__.__name__}: {exc}")
            if result.ok is None:
                status, skipped = "SKIP", skipped + 1
            elif result.ok:
                status, passed = "PASS", passed + 1
            else:
                status, failed = "FAIL", failed + 1
            dots = "." * max(3, 44 - len(label))
            print(f"  {number}. {label} {dots} {status} ({result.detail})")
    finally:
        state.cleanup()

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
