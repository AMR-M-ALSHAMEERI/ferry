"""The help screens wear Ferry's theme, not the help library's.

Reported by the human on the first run of ``ferry export --help``: typer's
stock cyan and yellow, and no mark, did not look like the program whose menu
they had just used.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import typer.rich_utils as rich_utils
from typer.testing import CliRunner

from ferry.cli import app, helpstyle
from ferry.cli.brand import TAGLINE
from ferry.cli.theme import THEMES, Capability

runner = CliRunner()

_TYPER_COLOURS = ("cyan", "yellow", "green", "magenta", "red")
"""Every colour name typer's defaults use. Harbor's colours are all hex."""


@pytest.fixture(autouse=True)
def _restore_typer_styles() -> Iterator[None]:
    """typer's styles are process-wide; put them back after each test."""
    names = [name for name in dir(rich_utils) if name.startswith("STYLE_")]
    names.append("COLOR_SYSTEM")
    saved = {name: getattr(rich_utils, name) for name in names}
    yield
    for name, value in saved.items():
        setattr(rich_utils, name, value)


def test_harbor_leaves_none_of_typers_own_colours_behind() -> None:
    helpstyle.apply(THEMES["harbor"])
    for name in dir(rich_utils):
        value = getattr(rich_utils, name)
        if name.startswith("STYLE_") and isinstance(value, str):
            assert not any(colour in value for colour in _TYPER_COLOURS), name


def test_harbor_draws_options_in_its_primary_and_switches_in_its_accent() -> None:
    harbor = THEMES["harbor"]
    helpstyle.apply(harbor)
    assert harbor.primary in rich_utils.STYLE_OPTION
    assert harbor.accent in rich_utils.STYLE_SWITCH
    assert rich_utils.COLOR_SYSTEM == "auto"


def test_mono_turns_escape_codes_off_and_a_colour_theme_turns_them_back_on() -> None:
    helpstyle.apply(THEMES["mono"])
    assert rich_utils.COLOR_SYSTEM is None
    helpstyle.apply(THEMES["compass"])
    assert rich_utils.COLOR_SYSTEM == "auto"


def test_the_theme_is_read_from_the_arguments_before_typer_parses_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FERRY_THEME", raising=False)
    monkeypatch.setattr(helpstyle, "detect_capability", lambda: Capability.COLOR)
    assert helpstyle.theme_for(["export", "--theme", "compass", "--help"]).name == "compass"
    assert helpstyle.theme_for(["--theme=classic"]).name == "classic"
    assert helpstyle.theme_for(["import", "--no-color", "--help"]).name == "mono"
    assert helpstyle.theme_for(["--theme", "neon"]).name == "harbor"


def test_the_top_level_help_opens_with_the_wordmark() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    # Not a terminal, so the ASCII form -- the same fallback the menu uses.
    assert "F E R R Y" in result.output
    assert TAGLINE in result.output
    assert result.output.index("F E R R Y") < result.output.index("Usage")


@pytest.mark.parametrize("command", ["export", "import", "compact", "tools"])
def test_a_command_help_names_itself_under_the_hull(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0
    first = next(line for line in result.output.splitlines() if line.strip())
    assert first.strip().endswith(f"ferry {command}")


def test_help_that_is_piped_holds_no_escape_codes() -> None:
    result = runner.invoke(app, ["import", "--help"])
    assert "\x1b[" not in result.output


def test_export_help_groups_sealing_apart() -> None:
    result = runner.invoke(app, ["export", "--help"])
    assert "Sealing" in result.output
    assert "FERRY_PASSPHRASE" in result.output
