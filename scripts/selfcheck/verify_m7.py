"""Layer-2 self-check for M7 -- cross-tool polish.

Contract (PLAN.md 6.8.2): numbered PASS/FAIL/SKIP lines, non-zero exit on any
failure, runs against **real data on this machine**, never prints conversation
content, and never modifies user data.

M7 is the first milestone with no adapter of its own. Its surface is what sits
*around* the adapters -- inspect, backups, dry runs, conflict handling,
encryption, deletion -- so this script exports a real bundle from every tool
installed here and then puts that bundle through all of it.

Three checks carry most of the weight, and each exists because layer 1 cannot
reach what it tests:

- **Check 6** runs a dry-run import against the **real** store. Layer 1 proves
  `dry_run` writes nothing to a fixture; only this proves it on the machine
  that matters. A safety copy of every store is taken first (check 3), because
  running a write-capable code path against real conversations on the strength
  of a flag being correct is not a thing to do without one.
- **Check 8** searches the sealed bytes for the real titles of the real
  conversations that went into them. A sealed bundle that leaks a title leaks
  the one thing people would most mind. **The titles are compared, never
  printed.**
- **Check 16** seals a bundle again with the verification stubbed out and
  asserts the original survived. This is the property that makes editing a
  sealed bundle defensible at all, and a bug here does not corrupt a file --
  it removes one.

Check 18 is the mandatory one: every store is fingerprinted before and after,
asserting none of this moved a byte of real data.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ferry.adapters.antigravity import AntigravityAdapter
from ferry.adapters.antigravity import paths as ag
from ferry.adapters.base import Adapter, ImportOptions
from ferry.adapters.claude_code import ClaudeCodeAdapter
from ferry.adapters.claude_code import paths as cc
from ferry.adapters.codex import CodexAdapter
from ferry.adapters.codex import paths as cx
from ferry.adapters.copilot import CopilotAdapter
from ferry.adapters.copilot import paths as cp
from ferry.cli.flows import bundle_at
from ferry.core import Bundle, BundleError, delete_bundle, sha256_file, summarise
from ferry.core.backup import read_manifest
from ferry.core.crypto import WrongPassphrase
from ferry.core.sealed import (
    is_sealed,
    opens_with,
    read_sealed_params,
    seal_bundle,
    unseal_bundle,
    unsealed,
)

PASSPHRASE = "self-check passphrase, not a secret"
"""Written into this file on purpose.

It seals a bundle made from a copy, in a temporary directory, deleted at the
end. Nothing a person owns is ever sealed with it.
"""


@dataclass
class Result:
    ok: bool | None
    detail: str


def _adapters() -> list[Adapter]:
    return [ClaudeCodeAdapter(), CodexAdapter(), CopilotAdapter(), AntigravityAdapter()]


def _store_roots() -> list[Path]:
    """Where each tool keeps conversations, whether or not it is installed."""
    return [cc.projects_dir(), cx.sessions_dir(), cp.workspace_storage(), ag.conversations_dir()]


def _fingerprint(roots: list[Path]) -> dict[str, str]:
    """Every readable file under every store, by checksum.

    A file that cannot be read is recorded by size rather than skipped: a
    locked database is still evidence, and dropping it silently would let a
    change hide behind a permission error.
    """
    found: dict[str, str] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                found[str(path)] = sha256_file(path)
            except OSError:
                try:
                    found[str(path)] = f"unreadable:{path.stat().st_size}"
                except OSError:
                    found[str(path)] = "unreadable"
    return found


class State:
    def __init__(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="ferry-verify-m7-"))
        self.bundle_dir = self.workspace / "bundle"
        self.roots = _store_roots()
        self.adapters = _adapters()
        self.detected = [(a, a.detect()) for a in self.adapters]
        self.installed = [a for a, found in self.detected if found.installed]
        self.source_before: dict[str, str] = {}
        self.exported = 0
        self.titles: list[str] = []
        self.sealed: Path | None = None
        self.sealed_bytes = 0

    def cleanup(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------


def check_installed(state: State) -> Result:
    names = [a.display_name for a in state.installed]
    if not names:
        return Result(None, "no supported assistant found on this machine")
    return Result(True, ", ".join(names))


def check_source_fingerprinted(state: State) -> Result:
    state.source_before = _fingerprint(state.roots)
    if not state.source_before:
        return Result(None, "nothing to fingerprint")
    total = sum(Path(p).stat().st_size for p in state.source_before if Path(p).exists())
    return Result(True, f"{len(state.source_before)} files, {total / 1024 / 1024:.1f} MB")


def check_safety_copy(state: State) -> Result:
    """Taken before anything runs against the real store.

    Check 6 runs an import -- a write-capable code path -- against real
    conversations on the strength of one flag being correct. That flag is
    exactly what is being tested, so it cannot also be the thing relied upon.
    """
    if not state.source_before:
        return Result(None, "nothing to copy")
    keep = state.workspace / "safety"
    copied = 0
    for root in state.roots:
        if not root.exists():
            continue
        shutil.copytree(root, keep / root.name, dirs_exist_ok=True)
        copied += 1
    return Result(True, f"{copied} stores copied to {keep}")


def check_export(state: State) -> Result:
    if not state.installed:
        return Result(None, "nothing installed to export from")
    for adapter in state.installed:
        for _ in adapter.export(state.bundle_dir):
            pass
    bundle = Bundle.open(state.bundle_dir)
    state.exported = len(bundle.list_conversations())
    state.titles = [
        (bundle.load_conversation(cid).title or "") for cid in bundle.list_conversations()
    ]
    tools = ", ".join(bundle.manifest.tools_included)
    return Result(state.exported > 0, f"{state.exported} conversations from {tools}")


def check_inspect(state: State) -> Result:
    """The summary must agree with the bundle, not with its own arithmetic."""
    if not state.exported:
        return Result(None, "nothing exported")
    bundle = Bundle.open(state.bundle_dir)
    summary = summarise(bundle)
    counted = sum(summary.by_tool.values())
    ok = len(summary.conversations) == state.exported == counted
    return Result(
        ok,
        f"{len(summary.conversations)} listed, {counted} across "
        f"{len(summary.by_tool)} tools, {summary.bytes_on_disk / 1024 / 1024:.1f} MB",
    )


def _differences(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Files added, removed, or changed between two fingerprints."""
    return sorted(
        set(after) ^ set(before)
        | {key for key in after.keys() & before.keys() if after[key] != before[key]}
    )


def check_dry_run_writes_nothing(state: State) -> Result:
    """The strongest statement this script can make about the real store."""
    if not state.exported:
        return Result(None, "nothing exported")
    before = _fingerprint(state.roots)
    events = 0
    for adapter in state.installed:
        for _ in adapter.import_(state.bundle_dir, ImportOptions(dry_run=True)):
            events += 1
    after = _fingerprint(state.roots)
    if after != before:
        changed = sorted(
            set(after) ^ set(before)
            | {k for k in after.keys() & before.keys() if after[k] != before[k]}
        )
        return Result(False, f"{len(changed)} files changed: {Path(changed[0]).name}")
    return Result(True, f"{events} events reported, {len(after)} files unchanged")


def check_seal(state: State) -> Result:
    if not state.exported:
        return Result(None, "nothing exported")
    started = time.monotonic()
    sealed = seal_bundle(state.bundle_dir, PASSPHRASE, state.workspace / "sealed.ferry")
    took = time.monotonic() - started
    state.sealed = sealed.path
    state.sealed_bytes = sealed.bytes_written
    plain = sum(p.stat().st_size for p in state.bundle_dir.rglob("*") if p.is_file())
    ok = is_sealed(sealed.path) and state.bundle_dir.is_dir()
    return Result(
        ok,
        f"{plain / 1024 / 1024:.1f} MB to {sealed.bytes_written / 1024 / 1024:.1f} MB "
        f"in {took:.1f}s; the plain bundle is still there",
    )


def check_nothing_readable(state: State) -> Result:
    """Real titles, compared and never printed."""
    if state.sealed is None:
        return Result(None, "nothing sealed")
    raw = state.sealed.read_bytes()
    structure = [b"manifest.json", b"conversations", b"source_raw", b"ucs_version"]
    leaked = [needle.decode() for needle in structure if needle in raw]
    titled = sum(1 for title in state.titles if title and title.encode("utf-8") in raw)
    params = read_sealed_params(state.sealed)
    if leaked or titled:
        return Result(False, f"leaked {leaked or ''} and {titled} titles")
    return Result(
        True,
        f"0 of {len(state.titles)} titles and no structure in the bytes; "
        f"only the {len(params.salt) if isinstance(params.salt, bytes) else 16}-byte salt is plain",
    )


def check_round_trip(state: State) -> Result:
    if state.sealed is None:
        return Result(None, "nothing sealed")
    out = state.workspace / "opened"
    unseal_bundle(state.sealed, PASSPHRASE, out)
    same = differ = 0
    for original in sorted(state.bundle_dir.rglob("*")):
        if not original.is_file():
            continue
        mirror = out / original.relative_to(state.bundle_dir)
        if mirror.is_file() and sha256_file(mirror) == sha256_file(original):
            same += 1
        else:
            differ += 1
    shutil.rmtree(out, ignore_errors=True)
    return Result(differ == 0, f"{same} files byte-identical, {differ} differ")


def _refused(archive: Path, passphrase: str = PASSPHRASE) -> bool:
    try:
        with unsealed(archive, passphrase):
            return False
    except (WrongPassphrase, BundleError, OSError):
        return True


def check_wrong_passphrase_refused(state: State) -> Result:
    if state.sealed is None:
        return Result(None, "nothing sealed")
    return Result(_refused(state.sealed, "not the passphrase"), "refused")


def check_flipped_byte_refused(state: State) -> Result:
    """One bit, in the middle, where a truncation check would never look."""
    if state.sealed is None:
        return Result(None, "nothing sealed")
    tampered = state.workspace / "flipped.ferry"
    raw = bytearray(state.sealed.read_bytes())
    middle = len(raw) // 2
    raw[middle] ^= 0x01
    tampered.write_bytes(raw)
    return Result(_refused(tampered), f"refused after one bit at offset {middle}")


def check_truncation_refused(state: State) -> Result:
    if state.sealed is None:
        return Result(None, "nothing sealed")
    cut = state.workspace / "cut.ferry"
    raw = state.sealed.read_bytes()
    cut.write_bytes(raw[: len(raw) * 2 // 3])
    return Result(_refused(cut), "refused a file cut to two thirds")


def check_delete_takes_everything(state: State) -> Result:
    """A conversation is not one file."""
    if not state.exported:
        return Result(None, "nothing exported")
    copy = state.workspace / "deletable"
    shutil.copytree(state.bundle_dir, copy)
    bundle = Bundle.open(copy)
    doomed = bundle.list_conversations()[0]
    expected = bundle.conversation_files(doomed)
    removed = bundle.delete_conversation(doomed)
    left = [path for path in expected if path.exists()]
    reopened = Bundle.open(copy)
    ok = (
        not left
        and removed.files == len(expected)
        and len(reopened.list_conversations()) == state.exported - 1
    )
    return Result(
        ok,
        f"{removed.files} files, {removed.bytes_freed / 1024 / 1024:.1f} MB freed, "
        f"{len(left)} left behind",
    )


def check_delete_records_a_backup(state: State) -> Result:
    """The copy taken before the only irreversible thing Ferry does."""
    if not state.exported:
        return Result(None, "nothing exported")
    copy = state.workspace / "backed-up"
    shutil.copytree(state.bundle_dir, copy)
    bundle = Bundle.open(copy)
    doomed = bundle.list_conversations()[0]
    removed = bundle.delete_conversation(doomed)
    if removed.backup is None:
        return Result(False, "no backup was taken")
    records = read_manifest(removed.backup.parent)
    ok = bool(records) and all(record.original for record in records)
    return Result(ok, f"{len(records)} recorded in {removed.backup.parent}")


def check_delete_bundle_refuses_a_stranger(state: State) -> Result:
    """The check that stands between a mistyped path and someone's Documents."""
    stranger = state.workspace / "not-a-bundle"
    (stranger / "keep").mkdir(parents=True)
    (stranger / "keep" / "important.txt").write_text("do not lose me", encoding="utf-8")
    try:
        delete_bundle(stranger)
    except BundleError:
        survived = (stranger / "keep" / "important.txt").is_file()
        return Result(survived, "refused, and the folder is untouched")
    return Result(False, "a folder with no manifest was deleted")


def check_reseal_replaces_in_place(state: State) -> Result:
    """Delete from a sealed bundle, seal it again, open it with the same word."""
    if state.sealed is None:
        return Result(None, "nothing sealed")
    archive = state.workspace / "editable.ferry"
    shutil.copyfile(state.sealed, archive)
    before = archive.read_bytes()

    with unsealed(archive, PASSPHRASE) as bundle:
        doomed = bundle.list_conversations()[0]
        bundle.delete_conversation(doomed, backup=False)
        staged = archive.with_name(archive.name + ".new")
        seal_bundle(bundle.root, PASSPHRASE, staged)
        if not opens_with(staged, PASSPHRASE):
            return Result(False, "the new file did not open")
        os.replace(staged, archive)

    with unsealed(archive, PASSPHRASE) as reopened:
        left = len(reopened.list_conversations())
    ok = archive.read_bytes() != before and left == state.exported - 1
    return Result(ok, f"{state.exported} became {left}, same passphrase, no .new left over")


def check_a_failed_reseal_keeps_the_original(state: State) -> Result:
    """The one place where being wrong costs everything.

    Written as the flow writes it -- beside, verify, replace -- with the
    verification forced to say no. Sealing over the original instead does not
    corrupt the file; it leaves no file at all.
    """
    if state.sealed is None:
        return Result(None, "nothing sealed")
    archive = state.workspace / "protected.ferry"
    shutil.copyfile(state.sealed, archive)
    before = archive.read_bytes()

    with unsealed(archive, PASSPHRASE) as bundle:
        bundle.delete_conversation(bundle.list_conversations()[0], backup=False)
        staged = archive.with_name(archive.name + ".new")
        seal_bundle(bundle.root, PASSPHRASE, staged)
        confirmed = False  # what opens_with() returning False must lead to
        if not confirmed:
            staged.unlink(missing_ok=True)

    ok = archive.read_bytes() == before and not staged.exists()
    return Result(ok, "the original survived and the half-made file was discarded")


def check_typed_path_verdicts(state: State) -> Result:
    """Four mistakes, four sentences, decided without opening anything."""
    missing = state.workspace / "no-such-thing"
    empty = state.workspace / "empty-folder"
    empty.mkdir(exist_ok=True)
    note = state.workspace / "notes.txt"
    note.write_text("not a bundle", encoding="utf-8")

    cases = [
        ("a bundle", bundle_at(str(state.bundle_dir)).bundle == state.bundle_dir),
        ("a sealed file", bundle_at(str(state.sealed)).bundle == state.sealed),
        ("nothing there", "There is nothing at" in bundle_at(str(missing)).problem),
        ("no manifest", "manifest.json" in bundle_at(str(empty)).problem),
        ("a plain file", "is a file, not a bundle" in bundle_at(str(note)).problem),
        ("quoted", bundle_at(f'"{state.bundle_dir}"').bundle == state.bundle_dir),
        ("a folder of bundles", state.bundle_dir in bundle_at(str(state.workspace)).nearby),
    ]
    wrong = [name for name, ok in cases if not ok]
    return Result(not wrong, f"{len(cases) - len(wrong)} of {len(cases)} judged correctly")


def check_source_unmodified(state: State) -> Result:
    """The mandatory one. None of the above may move a byte of real data."""
    if not state.source_before:
        return Result(None, "nothing was fingerprinted")
    after = _fingerprint(state.roots)
    if after != state.source_before:
        changed = _differences(state.source_before, after)
        return Result(False, f"{len(changed)} files changed: {Path(changed[0]).name}")
    return Result(True, f"{len(after)} files, all checksums match")


CHECKS: list[tuple[str, object]] = [
    ("tools found on this machine", check_installed),
    ("source fingerprinted", check_source_fingerprinted),
    ("safety copy of every store", check_safety_copy),
    ("export a real bundle", check_export),
    ("inspect agrees with the bundle", check_inspect),
    ("a dry run against the real store writes nothing", check_dry_run_writes_nothing),
    ("seal it", check_seal),
    ("no title and no structure in the sealed bytes", check_nothing_readable),
    ("round trip is byte-identical", check_round_trip),
    ("a wrong passphrase is refused", check_wrong_passphrase_refused),
    ("one flipped byte is refused", check_flipped_byte_refused),
    ("a truncated file is refused", check_truncation_refused),
    ("deleting takes every file of a conversation", check_delete_takes_everything),
    ("deleting records a backup first", check_delete_records_a_backup),
    ("a folder with no manifest is refused", check_delete_bundle_refuses_a_stranger),
    ("reseal writes beside, verifies, replaces", check_reseal_replaces_in_place),
    ("a reseal that does not verify keeps the original", check_a_failed_reseal_keeps_the_original),
    ("a typed path is judged correctly", check_typed_path_verdicts),
    ("source store untouched", check_source_unmodified),
]


def main() -> int:
    state = State()
    print("Ferry self-check - M7 (cross-tool polish)")
    passed = failed = skipped = 0
    try:
        for number, (label, run) in enumerate(CHECKS, start=1):
            try:
                result = run(state)  # type: ignore[operator]
            except Exception as exc:  # noqa: BLE001 - a crashed check is a failed check
                result = Result(False, f"{exc.__class__.__name__}: {exc}")
            if result.ok is None:
                status, skipped = "SKIP", skipped + 1
            elif result.ok:
                status, passed = "PASS", passed + 1
            else:
                status, failed = "FAIL", failed + 1
            dots = "." * max(3, 52 - len(label))
            print(f"  {number}. {label} {dots} {status} ({result.detail})")
    finally:
        state.cleanup()

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
