"""The SKILL.md that teaches an AI assistant to drive Ferry (PLAN.md §5 M8).

What matters most is that it never names a command or a flag Ferry does not
have: an assistant follows the file literally, and a flag that does not exist
is an error it will retry, rename, or work around by guessing. The rest checks
the file is one file - the copy at the repository root and the copy an
installed Ferry hands over are the same bytes - and that installing it is as
careful as everything else Ferry writes.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import typer.main
from typer.testing import CliRunner

from ferry.cli import app
from ferry.cli.commands import OK, REFUSED, skill_path
from ferry.core.backup import backup_root
from ferry.skill import SKILL_NAME, skill_text

ROOT = Path(__file__).parents[1]
DESCRIPTION_LIMIT = 1536
"""Claude Code truncates a skill's description beyond this in its listing."""

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


def test_the_copy_at_the_root_is_the_packaged_one() -> None:
    assert (ROOT / "SKILL.md").read_bytes() == (
        ROOT / "src" / "ferry" / "skill" / "SKILL.md"
    ).read_bytes()


def test_the_frontmatter_names_the_skill_and_fits_the_listing() -> None:
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


def test_install_puts_it_where_claude_code_looks() -> None:
    # conftest points CLAUDE_CONFIG_DIR at an empty temporary folder.
    expected = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "skills" / "ferry" / "SKILL.md"
    assert skill_path() == expected

    result = runner.invoke(app, ["skill", "--install"])
    assert result.exit_code == OK, result.output
    assert expected.read_text(encoding="utf-8") == skill_text()

    again = runner.invoke(app, ["skill", "--install"])
    assert again.exit_code == OK
    assert "Already installed" in again.output


def test_a_different_skill_is_kept_unless_forced_and_backed_up_when_it_is() -> None:
    target = skill_path()
    target.parent.mkdir(parents=True)
    target.write_text("the person's own edits\n", encoding="utf-8")

    refused = runner.invoke(app, ["skill", "--install"])
    assert refused.exit_code == REFUSED
    assert target.read_text(encoding="utf-8") == "the person's own edits\n"

    forced = runner.invoke(app, ["skill", "--install", "--force"])
    assert forced.exit_code == OK, forced.output
    assert target.read_text(encoding="utf-8") == skill_text()
    saved = [p.read_text(encoding="utf-8") for p in backup_root().rglob("SKILL.md")]
    assert saved == ["the person's own edits\n"]


def test_force_without_install_is_refused() -> None:
    result = runner.invoke(app, ["skill", "--force"])
    assert result.exit_code == 2
    assert not skill_path().exists()


@pytest.mark.parametrize("command", ["skill"])
def test_the_skill_command_help_wears_the_wordmark(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert "F E R R Y" in result.output
