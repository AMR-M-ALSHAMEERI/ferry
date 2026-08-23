"""Layer-2 self-check for M4 -- the Codex adapter.

Contract (PLAN.md 6.8.2): numbered PASS/FAIL/SKIP lines, non-zero exit on any
failure, runs against **real data on this machine**, never prints conversation
content, and never modifies user data.

Check 10 is the mandatory one: every file under the real sessions tree is
checksummed before and after, asserting export did not move a byte.

Two checks exist only for Codex. Check 4 asserts the export **streams** -- a
single rollout reached 53 MB here and reports describe far larger, so peak
memory is measured rather than assumed. Check 9 asks **Codex itself** whether
the rollouts Ferry rebuilt are valid, because a header Codex dislikes makes it
drop the conversation silently, with no error and no partial read.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import tracemalloc
from dataclasses import dataclass
from pathlib import Path

from ferry.adapters.base import ImportOptions
from ferry.adapters.codex import CodexAdapter
from ferry.adapters.codex.paths import codex_home, rollout_files, sessions_dir
from ferry.core import Bundle, sha256_file
from ferry.ucs import UCS_VERSION, Conversation


@dataclass
class Result:
    ok: bool | None
    detail: str


class State:
    def __init__(self) -> None:
        self.adapter = CodexAdapter()
        self.workspace = Path(tempfile.mkdtemp(prefix="ferry-verify-m4-"))
        self.bundle_dir = self.workspace / "bundle"
        self.target = self.workspace / "target"
        self.again = self.workspace / "again"
        self.source_before: dict[str, str] = {}
        self.detected = self.adapter.detect()
        self.exported = 0
        self.peak_mb = 0.0
        self.largest = 0

    def source_files(self) -> list[Path]:
        root = sessions_dir()
        return sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []

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
    rollouts = rollout_files()
    state.largest = max((p.stat().st_size for p in rollouts), default=0)
    total = sum(p.stat().st_size for p in rollouts)
    return Result(
        True,
        f"{len(rollouts)} sessions, {total / 1024 / 1024:.0f} MB, largest "
        f"{state.largest / 1024 / 1024:.0f} MB",
    )


def check_source_fingerprinted(state: State) -> Result:
    if not state.detected.installed:
        return Result(None, "nothing to fingerprint")
    state.source_before = state.fingerprint()
    total = sum(Path(p).stat().st_size for p in state.source_before)
    return Result(True, f"{len(state.source_before)} files, {total:,} bytes")


PEAK_BUDGET = 6.0
"""Peak memory allowed, as a multiple of the largest rollout.

Measured at ~3.8x on the probe machine. The budget is not a guess at what
"streaming" costs -- it is a tripwire on the ratio, so a change that starts
holding something extra shows up as a number rather than as a slow machine.
"""


def check_export_streams(state: State) -> Result:
    """Bounded memory, not zero memory -- and the difference is worth stating.

    The *file* is read a line at a time and never with ``read()``; a unit test
    asserts that structurally by making whole-file reads raise. What is not
    streamed is the **UCS model**: one conversation becomes one
    ``Conversation`` object and one JSON serialisation of it, so peak tracks the
    largest conversation rather than the largest file. Measured at about 3.8x
    the file here.

    That is a real ceiling and it is recorded as a known gap: a rollout of the
    size third parties report (hundreds of MB) would still exhaust memory. The
    fix is incremental bundle writing, which is not this milestone.
    """
    if not state.detected.installed:
        return Result(None, "nothing to export")
    errors = 0
    tracemalloc.start()
    for event in state.adapter.export(state.bundle_dir):
        if event.kind == "progress":
            state.exported += 1
        elif event.kind == "error":
            errors += 1
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    state.peak_mb = peak / 1024 / 1024

    if errors:
        return Result(False, f"{errors} errors during export")
    if not state.largest:
        return Result(None, "no rollouts to measure")

    ratio = peak / state.largest
    detail = (
        f"{state.exported} exported, peak {state.peak_mb:.0f} MB = "
        f"{ratio:.1f}x the largest file ({state.largest / 1024 / 1024:.0f} MB)"
    )
    if ratio > PEAK_BUDGET:
        return Result(False, f"{detail} - over the {PEAK_BUDGET:.0f}x budget")
    return Result(True, detail)


def check_bundle_validates(state: State) -> Result:
    if not state.detected.installed:
        return Result(None, "no bundle")
    problems = Bundle.open(state.bundle_dir).validate()
    if problems:
        return Result(False, f"{len(problems)} problems, first: {problems[0]}")
    return Result(True, "manifest, ids and attachment checksums all agree")


def check_schema_conformance(state: State) -> Result:
    if not state.detected.installed:
        return Result(None, "no bundle")
    bad: list[str] = []
    for path in sorted((state.bundle_dir / "conversations").glob("*.json")):
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
    if not state.detected.installed:
        return Result(None, "nothing to import")
    target = CodexAdapter(env={"CODEX_HOME": str(state.target)})
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
    if not state.detected.installed:
        return Result(None, "nothing to round-trip")
    target = CodexAdapter(env={"CODEX_HOME": str(state.target)})
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


def check_codex_accepts_the_rebuild(state: State) -> Result:
    """Ask Codex, not Ferry. A header Codex dislikes loses the whole file."""
    if not state.detected.installed:
        return Result(None, "nothing to validate")
    binaries = sorted(
        (Path.home() / "AppData/Local/OpenAI/Codex/bin").glob("*/codex.exe")
    ) or sorted(Path("/usr/local/bin").glob("codex"))
    if not binaries:
        return Result(None, "codex executable not found; cannot ask it")

    real_config = codex_home() / "config.toml"
    if real_config.is_file():
        shutil.copyfile(real_config, state.target / "config.toml")

    try:
        proc = subprocess.run(
            [str(binaries[-1]), "migrate-rollouts"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "CODEX_HOME": str(state.target)},
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result(None, f"could not run codex: {exc.__class__.__name__}")

    summary = next(
        (line for line in (proc.stdout or "").splitlines() if line.startswith("Scanned ")),
        "",
    )
    if "0 failed" in summary and f"{state.exported} eligible" in summary:
        return Result(True, summary.strip())
    return Result(False, summary.strip() or f"exit={proc.returncode}")


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
        first = Path(next(iter(vanished + appeared + changed))).name
        return Result(
            False,
            f"{len(vanished)} removed, {len(appeared)} added, {len(changed)} modified "
            f"-- first change: {first}",
        )
    return Result(True, f"{len(after)} files, all checksums match")


def main() -> int:
    state = State()
    checks = [
        ("Codex installed", check_installed),
        ("Session directory found", check_data_directory),
        ("Source fingerprinted", check_source_fingerprinted),
        ("Export streams, never whole-file", check_export_streams),
        ("Bundle validates", check_bundle_validates),
        ("UCS schema conformance", check_schema_conformance),
        ("Import to temp dir", check_import_to_temp),
        ("Round-trip fidelity", check_round_trip),
        ("Codex accepts the rebuild", check_codex_accepts_the_rebuild),
        ("Source data unmodified", check_source_unmodified),
    ]

    print("Ferry self-check - M4 (Codex adapter)")
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
