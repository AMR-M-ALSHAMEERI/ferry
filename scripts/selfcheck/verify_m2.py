"""Layer-2 self-check for M2 — the interactive CLI shell.

Contract (PLAN.md §6.8.2): numbered PASS/FAIL/SKIP lines, non-zero exit on any
failure, runs against the real installed package, never prints conversation
content, and never modifies user data.

M2 has no adapters yet, so nothing here touches a real conversation store. The
checks that matter are the ones a unit test cannot make: that the installed
`ferry` console script actually runs, and that redirected output is clean.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

CHECKS: list[tuple[str, str]] = []


@dataclass
class Result:
    ok: bool | None
    detail: str


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Invoke the CLI the way a user would — through the module entry point."""
    return subprocess.run(
        [sys.executable, "-m", "ferry", *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def check_entry_point_runs() -> Result:
    proc = _run(["--version"])
    version = proc.stdout.strip()
    if proc.returncode == 0 and version.startswith("ferry "):
        return Result(True, version)
    return Result(False, f"exit={proc.returncode} out={version!r}")


def check_help_lists_flags() -> Result:
    proc = _run(["--help"])
    missing = [f for f in ("--theme", "--no-color", "--verbose") if f not in proc.stdout]
    if proc.returncode == 0 and not missing:
        return Result(True, "--theme, --no-color, --verbose present")
    return Result(False, f"missing {missing}" if missing else f"exit={proc.returncode}")


def check_non_tty_exits_cleanly() -> Result:
    """The important one: no TTY must fail fast, not hang on a prompt."""
    try:
        proc = _run([])
    except subprocess.TimeoutExpired:
        return Result(False, "HUNG waiting for input with no terminal")
    if proc.returncode == 2 and "interactive terminal" in proc.stdout:
        return Result(True, "exit 2 with guidance, no hang")
    return Result(False, f"exit={proc.returncode}")


def check_redirected_output_is_ascii() -> Result:
    proc = _run(["tools"])
    bad = [c for c in proc.stdout if not c.isascii()]
    if not bad:
        return Result(True, f"{len(proc.stdout)} chars, all ASCII")
    return Result(False, f"non-ASCII: {sorted(set(bad))!r}")


def check_no_escape_codes_when_redirected() -> Result:
    proc = _run(["tools"])
    if "\x1b[" not in proc.stdout:
        return Result(True, "no ANSI sequences")
    return Result(False, "ANSI escape sequences reached redirected output")


def check_all_themes_accepted() -> Result:
    for name in ("harbor", "compass", "classic", "mono"):
        proc = _run(["--theme", name, "tools"])
        if proc.returncode not in (0, 1):
            return Result(False, f"--theme {name} exited {proc.returncode}")
    return Result(True, "harbor, compass, classic, mono all accepted")


def check_bad_theme_rejected() -> Result:
    proc = _run(["--theme", "neon", "tools"])
    combined = proc.stdout + proc.stderr
    if proc.returncode != 0 and "harbor" in combined:
        return Result(True, "rejected, valid names listed")
    return Result(False, f"exit={proc.returncode}")


def check_tools_lists_every_adapter() -> Result:
    proc = _run(["tools"])
    expected = ["Claude Code", "OpenAI Codex", "GitHub Copilot Chat", "Antigravity"]
    missing = [name for name in expected if name not in proc.stdout]
    if not missing:
        return Result(True, f"{len(expected)} adapters listed")
    return Result(False, f"missing {missing}")


def check_stubs_claim_nothing_installed() -> Result:
    """M2 must not invent conversation counts for adapters that do not exist."""
    from ferry.adapters.base import list_adapters

    lying = [a.name for a in list_adapters() if a.detect().installed]
    if not lying:
        return Result(True, "all adapters honestly report not-installed")
    return Result(False, f"stub claims to be installed: {lying}")


def check_icons_downgrade_on_legacy_encoding() -> Result:
    """A Windows console reporting cp1252 must not be sent Unicode glyphs —
    printing one there raises UnicodeEncodeError and kills the process."""
    import io

    from ferry.cli.theme import ASCII_ICONS, Capability, resolve_theme

    class _Cp1252(io.StringIO):
        encoding = "cp1252"

        def isatty(self) -> bool:
            return True

    theme = resolve_theme("harbor", capability=Capability.COLOR, env={}, stream=_Cp1252())
    if theme.icons is ASCII_ICONS and theme.uses_color:
        return Result(True, "ASCII icons, colour retained")
    return Result(False, f"icons={theme.icons.success!r} colour={theme.uses_color}")


def main() -> int:
    checks = [
        ("Entry point runs", check_entry_point_runs),
        ("Help lists the flags", check_help_lists_flags),
        ("No TTY exits cleanly, no hang", check_non_tty_exits_cleanly),
        ("Redirected output is pure ASCII", check_redirected_output_is_ascii),
        ("No escape codes when redirected", check_no_escape_codes_when_redirected),
        ("All four themes accepted", check_all_themes_accepted),
        ("Unknown theme rejected", check_bad_theme_rejected),
        ("Every adapter listed", check_tools_lists_every_adapter),
        ("Stubs report honestly", check_stubs_claim_nothing_installed),
        ("Icons downgrade on cp1252", check_icons_downgrade_on_legacy_encoding),
    ]

    print("Ferry self-check — M2 (interactive CLI shell)")
    passed = failed = skipped = 0
    for i, (label, fn) in enumerate(checks, start=1):
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - a crashed check is a failed check
            result = Result(False, f"{exc.__class__.__name__}: {exc}")
        if result.ok is None:
            status, skipped = "SKIP", skipped + 1
        elif result.ok:
            status, passed = "PASS", passed + 1
        else:
            status, failed = "FAIL", failed + 1
        dots = "." * max(3, 44 - len(label))
        print(f"  {i}. {label} {dots} {status} ({result.detail})")

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
