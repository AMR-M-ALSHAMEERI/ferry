"""The SKILL.md that teaches an AI assistant to drive Ferry.

What matters most is that it never names a command or a flag Ferry does not
have: an assistant follows the file literally, and a flag that does not exist
is an error it will retry, rename, or work around by guessing. The rest checks
the file is one file - the copy at the repository root and the copy an
installed Ferry hands over are the same bytes - and that installing it, into
each assistant's own folder, is as careful as everything else Ferry writes.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import typer.main
from typer.testing import CliRunner

from ferry.adapters import REGISTRY
from ferry.adapters.base import DetectResult
from ferry.cli import app
from ferry.cli.commands import FAILED, OK, REFUSED
from ferry.core.backup import backup_root
from ferry.skill import SKILL_NAME, SKILLS_HOME_ENV, skill_destinations, skill_text

ROOT = Path(__file__).parents[1]
DESCRIPTION_LIMIT = 1024
"""The strictest limit among the tools that read the skill: VS Code's.

Claude Code truncates at 1,536 in its listing; Codex budgets its whole list by
the model's context. VS Code refuses a description over 1,024.
"""

TOOLS = ("claude-code", "codex", "copilot", "antigravity")

runner = CliRunner()


def _frontmatter(text: str) -> dict[str, str]:
    """The YAML frontmatter, for the two keys used: plain and folded (``>-``) scalars."""
    assert text.startswith("---\n"), "SKILL.md must open with frontmatter"
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


def _cli() -> dict[str, set[str]]:
    """Every command, and the options it takes."""
    group = typer.main.get_command(app)
    return {
        name: {option for param in command.params for option in param.opts}
        for name, command in getattr(group, "commands", {}).items()
    }


def _written() -> set[str]:
    return {tool for tool, path in skill_destinations().items() if path.exists()}


def _said(result: object) -> str:
    return " ".join(str(getattr(result, "output", "")).split())


def _found(monkeypatch: pytest.MonkeyPatch, *tools: str) -> None:
    """Make Ferry find exactly these assistants."""
    for tool in tools:
        monkeypatch.setattr(REGISTRY[tool], "detect", lambda: DetectResult(installed=True))


# ---------------------------------------------------------------- the file


def test_the_copy_at_the_root_is_the_packaged_one() -> None:
    assert (ROOT / "SKILL.md").read_bytes() == (
        ROOT / "src" / "ferry" / "skill" / "SKILL.md"
    ).read_bytes()


def test_the_frontmatter_names_the_skill_and_fits_every_listing() -> None:
    fields = _frontmatter(skill_text())
    assert fields["name"] == SKILL_NAME
    assert 0 < len(fields["description"]) <= DESCRIPTION_LIMIT


def test_it_is_plain_ascii() -> None:
    """Printed by `ferry skill`, and a cp1252 console cannot encode much else."""
    assert skill_text().isascii()


def test_every_command_it_names_exists() -> None:
    named = set(re.findall(r"\bferry ([a-z]+)\b", skill_text()))
    assert named, "found no commands at all - the pattern is wrong"
    assert named <= set(_cli())


def test_every_flag_it_names_exists() -> None:
    text = skill_text()
    named = set(re.findall(r"`(--[a-z][a-z-]*)", text)) | set(re.findall(r"`(-[a-z])`", text))
    offered = set().union(*_cli().values())
    assert named, "found no flags at all - the pattern is wrong"
    assert named - offered == set()


def test_ferry_skill_prints_exactly_the_file() -> None:
    result = runner.invoke(app, ["skill"])
    assert result.exit_code == 0
    assert result.output == skill_text()


# ---------------------------------------------------------------- where it goes


def test_each_assistant_has_the_folder_its_documentation_names() -> None:
    home = Path(os.environ[SKILLS_HOME_ENV])
    antigravity = Path(os.environ["FERRY_ANTIGRAVITY_DIR"]).parent
    assert skill_destinations() == {
        "claude-code": Path(os.environ["CLAUDE_CONFIG_DIR"]) / "skills" / "ferry" / "SKILL.md",
        "codex": home / ".agents" / "skills" / "ferry" / "SKILL.md",
        "copilot": home / ".copilot" / "skills" / "ferry" / "SKILL.md",
        "antigravity": antigravity / "config" / "skills" / "ferry" / "SKILL.md",
    }


@pytest.mark.parametrize("tool", TOOLS)
def test_naming_a_tool_installs_for_it_alone(tool: str) -> None:
    result = runner.invoke(app, ["skill", "--install", "--tool", tool])
    assert result.exit_code == OK, result.output
    assert _written() == {tool}
    assert skill_destinations()[tool].read_text(encoding="utf-8") == skill_text()

    again = runner.invoke(app, ["skill", "--install", "--tool", tool])
    assert again.exit_code == OK
    assert "already installed" in _said(again)


def test_with_no_tool_and_no_assistant_found_nothing_is_written() -> None:
    result = runner.invoke(app, ["skill", "--install"])
    assert result.exit_code == REFUSED
    assert "--tool" in _said(result)
    assert _written() == set()


def test_with_no_tool_only_the_assistants_found_get_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _found(monkeypatch, "antigravity")
    result = runner.invoke(app, ["skill", "--install"])
    assert result.exit_code == OK, result.output
    assert _written() == {"antigravity"}


@pytest.mark.parametrize("args", [["--install"], ["--install", "--tool", "all"]])
def test_copilot_shares_the_copies_it_already_reads(
    args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """VS Code reads the Claude Code and Codex folders, so a copy of its own is a duplicate."""
    _found(monkeypatch, *TOOLS)
    result = runner.invoke(app, ["skill", *args])
    assert result.exit_code == OK, result.output
    assert _written() == {"claude-code", "codex", "antigravity"}
    assert "Copilot" in _said(result)


def test_copilot_alone_gets_a_copy_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    _found(monkeypatch, "copilot")
    result = runner.invoke(app, ["skill", "--install"])
    assert result.exit_code == OK, result.output
    assert _written() == {"copilot"}


# ---------------------------------------------------------------- carefully


def test_a_different_skill_is_kept_unless_forced_and_backed_up_when_it_is() -> None:
    target = skill_destinations()["claude-code"]
    target.parent.mkdir(parents=True)
    target.write_text("the person's own edits\n", encoding="utf-8")

    refused = runner.invoke(app, ["skill", "--install", "--tool", "claude-code"])
    assert refused.exit_code == REFUSED
    assert target.read_text(encoding="utf-8") == "the person's own edits\n"

    forced = runner.invoke(app, ["skill", "--install", "--tool", "claude-code", "--force"])
    assert forced.exit_code == OK, forced.output
    assert target.read_text(encoding="utf-8") == skill_text()
    saved = [p.read_text(encoding="utf-8") for p in backup_root().rglob("SKILL.md")]
    assert saved == ["the person's own edits\n"]


def test_one_kept_copy_does_not_stop_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    _found(monkeypatch, "claude-code", "codex")
    kept = skill_destinations()["claude-code"]
    kept.parent.mkdir(parents=True)
    kept.write_text("mine\n", encoding="utf-8")

    result = runner.invoke(app, ["skill", "--install"])
    assert result.exit_code == FAILED
    assert kept.read_text(encoding="utf-8") == "mine\n"
    assert skill_destinations()["codex"].read_text(encoding="utf-8") == skill_text()


@pytest.mark.parametrize("args", [["--force"], ["--tool", "codex"], ["--install", "--tool", "x"]])
def test_a_flag_that_makes_no_sense_is_refused(args: list[str]) -> None:
    result = runner.invoke(app, ["skill", *args])
    assert result.exit_code == 2
    assert _written() == set()


def test_the_skill_command_help_wears_the_wordmark() -> None:
    result = runner.invoke(app, ["skill", "--help"])
    assert "F E R R Y" in result.output
