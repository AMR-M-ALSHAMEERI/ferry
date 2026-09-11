"""Ferry's own look for ``--help`` and for command-line errors.

The help screens are drawn by typer, which brings its own palette -- cyan
options, yellow usage, green switches -- and no heading at all. Beside the menu
that read as a different program, and the person who first ran
``ferry export --help`` said so. This puts the menu's theme on it, and the
menu's mark at the top, so the help looks like it came from the thing that
drew the menu.

typer reads its styles from module-level constants in :mod:`typer.rich_utils`
every time it draws, so the theme is applied by setting them, once per run,
before any argument is parsed -- help is drawn *during* parsing, before
``--theme`` or ``--no-color`` have reached a callback of their own.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

import typer.rich_utils as rich_utils
from rich.console import Console
from rich.text import Text

# typer carries its own copy of click rather than depending on it, so the
# context and formatter types are typer's, not an installed click's.
from typer import _click as click
from typer.core import TyperCommand, TyperGroup

from ferry import __version__
from ferry.cli.brand import build_wordmark
from ferry.cli.theme import THEMES, Capability, Theme, detect_capability, resolve_theme

__all__ = ["FerryCommand", "FerryGroup", "apply", "still", "styles", "theme_for"]

_active: Theme | None = None
"""The theme applied for this run. ``None`` until :func:`apply` has run."""


def _bold(colour: str) -> str:
    return f"bold {colour}" if colour else ""


def styles(theme: Theme) -> dict[str, object]:
    """typer's style constants, as ``theme`` would have them.

    Every colour typer ships with is replaced, not only the ones that show on
    an ordinary help page: a usage error draws its own panel, and it should not
    be the one place the old palette survives.
    """
    return {
        "STYLE_OPTION": _bold(theme.primary),
        "STYLE_SWITCH": _bold(theme.accent),
        "STYLE_NEGATIVE_OPTION": _bold(theme.warning),
        "STYLE_NEGATIVE_SWITCH": _bold(theme.warning),
        "STYLE_TYPES": theme.dim,
        "STYLE_TYPES_SEPARATOR": theme.dim,
        "STYLE_USAGE": theme.primary,
        "STYLE_USAGE_COMMAND": _bold(theme.text),
        "STYLE_DEPRECATED": theme.error,
        "STYLE_DEPRECATED_COMMAND": theme.dim,
        "STYLE_HELPTEXT_FIRST_LINE": theme.text,
        "STYLE_HELPTEXT": theme.dim,
        "STYLE_OPTION_HELP": theme.text,
        "STYLE_OPTION_DEFAULT": theme.dim,
        "STYLE_OPTION_ENVVAR": theme.accent,
        "STYLE_REQUIRED_SHORT": theme.warning,
        "STYLE_REQUIRED_LONG": theme.warning,
        "STYLE_OPTIONS_PANEL_BORDER": theme.dim,
        "STYLE_COMMANDS_PANEL_BORDER": theme.dim,
        "STYLE_COMMANDS_TABLE_FIRST_COLUMN": _bold(theme.primary),
        "STYLE_ERRORS_PANEL_BORDER": theme.error,
        "STYLE_ERRORS_SUGGESTION": theme.dim,
        "STYLE_ABORTED": theme.error,
        # ``mono`` means no escape codes at all, not merely no colour: bold is
        # an escape code too, and a log file should hold none.
        "COLOR_SYSTEM": "auto" if theme.uses_color else None,
    }


def apply(theme: Theme) -> None:
    """Put ``theme`` on everything typer draws from here on."""
    global _active
    for name, value in styles(theme).items():
        setattr(rich_utils, name, value)
    _active = theme


def theme_for(args: Sequence[str]) -> Theme:
    """The theme this run asked for, read from the raw arguments.

    Honours ``--theme NAME``, ``--theme=NAME`` and ``--no-color`` wherever
    they appear, then the same order as everywhere else: ``FERRY_THEME``, the
    saved choice, ``harbor``. An unknown name is ignored here rather than
    refused; refusing it is the flag's own callback's job, and it does so with
    the valid names listed.
    """
    capability = detect_capability()
    requested: str | None = None
    items = list(args)
    for index, arg in enumerate(items):
        if arg == "--no-color":
            capability = Capability.NO_COLOR
        elif arg == "--theme" and index + 1 < len(items):
            requested = items[index + 1]
        elif arg.startswith("--theme="):
            requested = arg.partition("=")[2]
    if requested is not None and requested.strip().lower() not in THEMES:
        requested = None
    return resolve_theme(requested, capability=capability)


def still(theme: Theme, *, version: str = __version__) -> Text:
    """The whole wordmark, drawn once, in ``theme``'s own colours.

    The menu's banner is styled by name through the UI's console. The help is
    printed by a plain console that knows none of those names, so the same
    mark is built here with the colours themselves.
    """
    mark = build_wordmark(theme, version=version)
    out = Text()
    for index, mark_row in enumerate(mark.mark_rows(0)):
        out.append("  ")
        out.append(mark_row, style=theme.accent)
        out.append("  ")
        out.append(mark.letter_rows[index], style=theme.heading)
        if index == 0:
            out.append(f"   {mark.version}", style=theme.dim)
        out.append("\n")
    out.append("  ")
    out.append(mark.tagline, style=theme.dim)
    return out


def _console() -> Console:
    """A console that writes where typer's does, with the colour it is allowed."""
    return Console(
        color_system=rich_utils.COLOR_SYSTEM,
        force_terminal=rich_utils.FORCE_TERMINAL,
        width=rich_utils.MAX_WIDTH,
        highlight=False,
    )


def _header(ctx: click.Context, *, full: bool) -> None:
    """The top of a help page: the whole wordmark for ``ferry``, one line for a command.

    A command's page gets the hull and its own name rather than the whole mark
    again. Someone reading ``ferry import --help`` has already met the
    wordmark; what they need is to see they are in the right command.
    """
    theme = _active or theme_for(())
    console = _console()
    console.print()
    if full:
        console.print(still(theme))
        return
    line = Text("  ")
    line.append(build_wordmark(theme).hull_rows[-1], style=theme.accent)
    line.append("  ")
    line.append(ctx.command_path, style=theme.heading)
    console.print(line)


class FerryCommand(TyperCommand):
    """A command whose help page opens with Ferry's mark."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        _header(ctx, full=False)
        super().format_help(ctx, formatter)


class FerryGroup(TyperGroup):
    """The ``ferry`` group: applies the theme before parsing, and heads its help."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        _header(ctx, full=True)
        super().format_help(ctx, formatter)

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        apply(theme_for(sys.argv[1:] if args is None else args))
        return super().main(
            args=args,
            prog_name=prog_name,
            complete_var=complete_var,
            standalone_mode=standalone_mode,
            windows_expand_args=windows_expand_args,
            **extra,
        )
