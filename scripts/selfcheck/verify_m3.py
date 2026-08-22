"""Layer-2 self-check for M3 -- the Claude Code adapter.

Contract (PLAN.md 6.8.2): numbered PASS/FAIL/SKIP lines, non-zero exit on any
failure, runs against **real data on this machine**, never prints conversation
content, and never modifies user data.

Read that last clause twice. This script opens the user's actual conversation
history. It reads; it writes only into a temporary directory it made itself;
and check 9 exists to prove that -- it checksums every file under the real
Claude Code directory before and after everything else has run, and fails if a
single byte moved. PLAN.md calls that check mandatory in every adapter
self-check and it is the most important line in the file.

Nothing here prints a message, a title, or any part of a conversation. Counts,
ids, field names and sizes only.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ferry.adapters.base import ImportOptions
from ferry.adapters.claude_code import ClaudeCodeAdapter
from ferry.adapters.claude_code.paths import mangle, projects_dir
from ferry.core import Bundle, sha256_file
from ferry.ucs import UCS_VERSION, Conversation


@dataclass
class Result:
    ok: bool | None
    detail: str


class State:
    """What each check hands to the next."""

    def __init__(self) -> None:
        self.adapter = ClaudeCodeAdapter()
        self.workspace = Path(tempfile.mkdtemp(prefix="ferry-verify-m3-"))
        self.bundle_dir = self.workspace / "bundle"
        self.target = self.workspace / "target"
        self.again = self.workspace / "again"
        self.source_before: dict[str, str] = {}
        self.detected = self.adapter.detect()
        self.exported = 0
        self.errors = 0

    def source_files(self) -> list[Path]:
        root = projects_dir()
        if not root.is_dir():
            return []
        return sorted(p for p in root.rglob("*") if p.is_file())

    def fingerprint(self) -> dict[str, str]:
        return {str(p): sha256_file(p) for p in self.source_files()}

    def cleanup(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)


def check_installed(state: State) -> Result:
    if not state.detected.installed:
        return Result(None, "; ".join(state.detected.notes) or "not installed")
    return Result(True, f"v{state.detected.version}")


def check_data_directory(state: State) -> Result:
    if not state.detected.installed:
        return Result(None, "no data to find")
    projects = {p.parent for p in state.source_files() if p.suffix == ".jsonl"}
    return Result(
        True,
        f"{len(projects)} projects, {state.detected.conversation_count_estimate} sessions",
    )


def check_source_fingerprinted(state: State) -> Result:
    """Taken before anything else runs, so check 9 has something to compare to."""
    if not state.detected.installed:
        return Result(None, "nothing to fingerprint")
    state.source_before = state.fingerprint()
    total = sum(Path(p).stat().st_size for p in state.source_before)
    return Result(True, f"{len(state.source_before)} files, {total:,} bytes")


def check_export(state: State) -> Result:
    if not state.detected.installed:
        return Result(None, "nothing to export")
    skipped = 0
    for event in state.adapter.export(state.bundle_dir):
        if event.kind == "progress":
            state.exported += 1
        elif event.kind == "error":
            state.errors += 1
        elif event.kind == "skipped":
            skipped += 1
    if state.errors:
        return Result(False, f"{state.errors} errors during export")
    return Result(True, f"{state.exported} exported, {skipped} skipped, 0 errors")


def check_bundle_validates(state: State) -> Result:
    if not state.detected.installed:
        return Result(None, "no bundle")
    problems = Bundle.open(state.bundle_dir).validate()
    if problems:
        return Result(False, f"{len(problems)} problems, first: {problems[0]}")
    return Result(True, "manifest, ids and attachment checksums all agree")


def check_schema_conformance(state: State) -> Result:
    """Re-validate each file against the model, independently of the exporter."""
    if not state.detected.installed:
        return Result(None, "no bundle")
    root = state.bundle_dir / "conversations"
    bad: list[str] = []
    for path in sorted(root.glob("*.json")):
        try:
            conversation = Conversation.model_validate_json(path.read_bytes())
        except Exception as exc:  # noqa: BLE001 - the failure is the finding
            bad.append(f"{path.stem}: {exc.__class__.__name__}")
            continue
        if conversation.ucs_version != UCS_VERSION:
            bad.append(f"{path.stem}: ucs_version {conversation.ucs_version}")
    if bad:
        return Result(False, f"{len(bad)} non-conforming, first: {bad[0]}")
    return Result(True, f"{state.exported}/{state.exported} valid UCS v{UCS_VERSION}")


def check_import_to_temp(state: State) -> Result:
    """Into a config directory of our own. The real one is never a target."""
    if not state.detected.installed:
        return Result(None, "nothing to import")
    target = ClaudeCodeAdapter(env={"CLAUDE_CONFIG_DIR": str(state.target)})
    written = errors = 0
    for event in target.import_(state.bundle_dir, ImportOptions()):
        if event.kind == "progress":
            written += 1
        elif event.kind == "error":
            errors += 1
    if errors or written != state.exported:
        return Result(False, f"{written}/{state.exported} written, {errors} errors")
    return Result(True, f"{written}/{state.exported} written to a temp directory")


def check_round_trip(state: State) -> Result:
    """Export the imported copy again and compare. A lossy mapping shows here."""
    if not state.detected.installed:
        return Result(None, "nothing to round-trip")
    target = ClaudeCodeAdapter(env={"CLAUDE_CONFIG_DIR": str(state.target)})
    for _ in target.export(state.again):
        pass
    first, second = Bundle.open(state.bundle_dir), Bundle.open(state.again)
    if first.list_conversations() != second.list_conversations():
        return Result(False, "the second export holds a different set of conversations")

    identical = differing = 0
    for conversation_id in first.list_conversations():
        before = first.load_conversation(conversation_id).model_dump_json()
        after = second.load_conversation(conversation_id).model_dump_json()
        if before == after:
            identical += 1
        else:
            differing += 1
    if differing:
        return Result(False, f"{differing} conversations changed across the round trip")
    return Result(True, f"{identical}/{identical} identical")


def check_path_remap(state: State) -> Result:
    """The name is derived from the new path, never decoded from the old one.

    The expected directory name is spelled out here rather than computed, so
    this asserts against the rule rather than against the implementation.
    """
    if not state.detected.installed:
        return Result(None, "nothing to remap")
    bundle = Bundle.open(state.bundle_dir)
    ids = bundle.list_conversations()
    if not ids:
        return Result(None, "no conversations in the bundle")

    conversation = bundle.load_conversation(ids[0])
    original = conversation.workspace.original_path
    if not original:
        return Result(None, "the bundle records no working directory")

    remapped = state.workspace / "remapped"
    target = ClaudeCodeAdapter(env={"CLAUDE_CONFIG_DIR": str(remapped)})
    new_cwd = "/home/ferry-verify/probe_a.b-c"
    for _ in target.import_(state.bundle_dir, ImportOptions(path_remap=((original, new_cwd),))):
        pass

    expected = "-home-ferry-verify-probe-a-b-c"
    landed = remapped / "projects" / expected / f"{ids[0]}.jsonl"
    if not landed.is_file():
        found = [p.name for p in (remapped / "projects").iterdir()] if remapped.is_dir() else []
        return Result(False, f"expected {expected}, found {found}")
    if mangle(new_cwd) != expected:
        return Result(False, f"mangle disagrees: {mangle(new_cwd)}")
    return Result(True, f"{expected}")


def check_source_unmodified(state: State) -> Result:
    """MANDATORY (PLAN.md 6.8.2). Export is read-only or it is a data-loss bug."""
    if not state.detected.installed:
        return Result(None, "nothing to protect")
    after = state.fingerprint()

    vanished = sorted(set(state.source_before) - set(after))
    appeared = sorted(set(after) - set(state.source_before))
    changed = sorted(
        p for p in set(after) & set(state.source_before) if after[p] != state.source_before[p]
    )

    if vanished or appeared or changed:
        return Result(
            False,
            f"{len(vanished)} removed, {len(appeared)} added, {len(changed)} modified -- "
            f"first change: {Path(next(iter(vanished + appeared + changed))).name}",
        )
    return Result(True, f"{len(after)} files, all checksums match")


def main() -> int:
    state = State()
    checks = [
        ("Claude Code installed", check_installed),
        ("Data directory found", check_data_directory),
        ("Source fingerprinted", check_source_fingerprinted),
        ("Export all conversations", check_export),
        ("Bundle validates", check_bundle_validates),
        ("UCS schema conformance", check_schema_conformance),
        ("Import to temp dir", check_import_to_temp),
        ("Round-trip fidelity", check_round_trip),
        ("Path remap correctness", check_path_remap),
        ("Source data unmodified", check_source_unmodified),
    ]

    print("Ferry self-check - M3 (Claude Code adapter)")
    passed = failed = skipped = 0
    try:
        for number, (label, run) in enumerate(checks, start=1):
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
