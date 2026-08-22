"""Ferry's command-line entry point.

Running bare ``ferry`` is the primary path: it scans for installed assistants
and drops into an interactive menu. Flags exist underneath for scripting and for
the future VS Code extension, but they are deliberately the secondary route
(PLAN.md §5 M2).
"""

from __future__ import annotations

import typer

from ferry import __version__
from ferry.cli.menu import run_menu
from ferry.cli.theme import THEMES, Capability, detect_capability, resolve_theme
from ferry.cli.ui import UI

__all__ = ["app", "main"]

app = typer.Typer(
    name="ferry",
    help="Back up and migrate your local AI assistant conversation history.",
    invoke_without_command=True,
    no_args_is_help=False,
    add_completion=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"ferry {__version__}")
        raise typer.Exit()


def _theme_callback(value: str | None) -> str | None:
    """Reject an unknown theme name early, with the valid ones listed."""
    if value is None:
        return None
    if value.strip().lower() not in THEMES:
        valid = ", ".join(sorted(THEMES))
        raise typer.BadParameter(f"unknown theme {value!r}. Choose one of: {valid}")
    return value.strip().lower()


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
    theme: str | None = typer.Option(
        None,
        "--theme",
        callback=_theme_callback,
        help="Colour theme: harbor, compass, classic, or mono.",
    ),
    no_color: bool = typer.Option(
        False, "--no-color", help="Disable colour and use plain ASCII markers."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show extra detail about what Ferry is doing."
    ),
) -> None:
    """Ferry — back up and migrate your AI assistant conversation history."""
    if ctx.invoked_subcommand is not None:
        return

    capability = Capability.NO_COLOR if no_color else detect_capability()
    active = resolve_theme(theme, capability=capability)
    ui = UI(active, capability=capability, verbose=verbose)

    if not ui.interactive:
        ui.error("Ferry needs an interactive terminal for its menu.")
        ui.info("Output is being piped or redirected, so prompts cannot be shown.")
        ui.info("Run `ferry --help` to see the available commands and flags.")
        raise typer.Exit(2)

    raise typer.Exit(run_menu(ui))


@app.command("tools")
def tools(
    theme: str | None = typer.Option(None, "--theme", callback=_theme_callback),
    no_color: bool = typer.Option(False, "--no-color"),
) -> None:
    """List the AI assistants Ferry can detect on this machine.

    Works without a terminal, so it is the one thing that is useful in a script
    today. Exits non-zero when nothing is detected.
    """
    from ferry.cli.menu import scan

    capability = Capability.NO_COLOR if no_color else detect_capability()
    ui = UI(resolve_theme(theme, capability=capability), capability=capability)
    results = scan(ui)
    raise typer.Exit(0 if any(r.installed for _, r in results) else 1)
