"""Layer-2 self-check for M8 -- the SKILL.md file.

Contract (PLAN.md 6.8.2): numbered PASS/FAIL/SKIP lines, non-zero exit on any
failure, runs against **this machine's real install**, never prints
conversation content, and never modifies user data.

M8's row in the self-check table: *SKILL.md frontmatter parses; every CLI
command named in it actually exists (parse ``--help`` and cross-check)*. That is
checks 1, 3 and 4. They go through the installed ``ferry`` in a subprocess
rather than importing the CLI, because an assistant following the file meets
the command line, not the Python objects behind it.

Check 6 reads the copy installed for Claude Code, if there is one, and only
reads it: a stale skill is reported, never rewritten.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ferry.cli.commands import skill_path
from ferry.skill import SKILL_NAME, skill_text

ROOT = Path(__file__).resolve().parents[2]
DESCRIPTION_LIMIT = 1536


@dataclass
class Result:
    ok: bool | None
    detail: str


def _ferry(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the installed ``ferry`` as an assistant would: piped, no colour, wide."""
    env = dict(os.environ, NO_COLOR="1", TERMINAL_WIDTH="200", PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "ferry", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
        check=False,
    )


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        raise ValueError("does not open with frontmatter")
    block = text.split("---\n", 2)[1]
    fields: dict[str, str] = {}
    key = ""
    for line in block.splitlines():
        if line.startswith(" ") and key:
            fields[key] = (fields[key] + " " + line.strip()).strip()
        elif ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            fields[key] = "" if value.strip() == ">-" else value.strip()
    return fields


def check_frontmatter() -> Result:
    fields = _frontmatter(skill_text())
    name, description = fields.get("name", ""), fields.get("description", "")
    if name != SKILL_NAME:
        return Result(False, f"name is {name!r}, expected {SKILL_NAME!r}")
    if not description or len(description) > DESCRIPTION_LIMIT:
        return Result(False, f"description is {len(description)} characters")
    return Result(True, f"name {name}, description {len(description)}/{DESCRIPTION_LIMIT}")


def check_one_file() -> Result:
    root_copy = ROOT / "SKILL.md"
    if not root_copy.is_file():
        return Result(None, "no repository checkout here")
    same = root_copy.read_text(encoding="utf-8") == skill_text()
    return Result(same, "root and packaged copies identical" if same else "the two copies differ")


def check_commands() -> Result:
    named = sorted(set(re.findall(r"\bferry ([a-z]+)\b", skill_text())))
    missing = [name for name in named if _ferry(name, "--help").returncode != 0]
    if missing:
        return Result(False, f"no such command: {', '.join(missing)}")
    return Result(True, f"{len(named)} commands, each answers --help")


def check_flags() -> Result:
    text = skill_text()
    named = set(re.findall(r"`(--[a-z][a-z-]*)", text)) | set(re.findall(r"`(-[a-z])`", text))
    commands = sorted(set(re.findall(r"\bferry ([a-z]+)\b", text)))
    helps = " ".join(_ferry(name, "--help").stdout for name in commands)
    offered = set(re.findall(r"(?<![\w-])(--?[a-z][a-z-]*)", helps))
    missing = sorted(named - offered)
    if missing:
        return Result(False, f"not in any --help: {', '.join(missing)}")
    return Result(True, f"{len(named)} flags, each in a command's --help")


def check_printed() -> Result:
    run = _ferry("skill")
    if run.returncode != 0:
        return Result(False, f"ferry skill exited {run.returncode}")
    same = run.stdout.replace("\r\n", "\n") == skill_text()
    return Result(same, "ferry skill prints the file" if same else "printed text differs")


def check_installed() -> Result:
    target = skill_path()
    if not target.is_file():
        return Result(None, "not installed for Claude Code on this machine")
    current = target.read_text(encoding="utf-8", errors="replace")
    if current == skill_text():
        return Result(True, "installed copy is current")
    return Result(False, "installed copy is out of date - ferry skill --install --force")


CHECKS = [
    ("the frontmatter parses", check_frontmatter),
    ("the repository and the package hold one file", check_one_file),
    ("every command it names exists", check_commands),
    ("every flag it names is in --help", check_flags),
    ("ferry skill prints it unchanged", check_printed),
    ("the copy installed for Claude Code", check_installed),
]


def main() -> int:
    print("Ferry self-check - M8 (SKILL.md)")
    passed = failed = skipped = 0
    for number, (label, run) in enumerate(CHECKS, start=1):
        try:
            result = run()
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

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
