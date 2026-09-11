"""Ferry's command-line entry point.

Running bare ``ferry`` is the primary path: it scans for installed assistants
and drops into an interactive menu. Flags exist underneath for scripting and for
the future VS Code extension, but they are deliberately the secondary route
(PLAN.md §5 M2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import typer

from ferry import __version__
from ferry.cli.helpstyle import FerryCommand, FerryGroup
from ferry.cli.menu import run_menu
from ferry.cli.theme import THEMES, Capability, detect_capability, resolve_theme
from ferry.cli.ui import UI

if TYPE_CHECKING:
    from ferry.adapters.base import ConversionMode, OnConflict

__all__ = ["app", "main"]

app = typer.Typer(
    name="ferry",
    cls=FerryGroup,
    help="Back up and migrate your local AI assistant conversation history.",
    epilog="Run ferry on its own for the menu. The commands above do the same work from a script.",
    invoke_without_command=True,
    no_args_is_help=False,
    add_completion=True,
)

_APPEARANCE = "Appearance"
_SEALING = "Sealing"
_CROSSING = "Crossing tools"

_TOOL_NAMES = "claude-code, codex, copilot or antigravity"

_THEME_HELP = "Colour theme: harbor, compass, classic or mono."
_NO_COLOR_HELP = "No colour, and plain ASCII markers."


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
        metavar="NAME",
        callback=_theme_callback,
        help=_THEME_HELP,
        rich_help_panel=_APPEARANCE,
    ),
    no_color: bool = typer.Option(
        False, "--no-color", help=_NO_COLOR_HELP, rich_help_panel=_APPEARANCE
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show extra detail about what Ferry is doing."
    ),
) -> None:
    """Ferry - back up and migrate your AI assistant conversation history."""
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


@app.command("tools", cls=FerryCommand)
def tools(
    theme: str | None = typer.Option(
        None,
        "--theme",
        metavar="NAME",
        callback=_theme_callback,
        help=_THEME_HELP,
        rich_help_panel=_APPEARANCE,
    ),
    no_color: bool = typer.Option(
        False, "--no-color", help=_NO_COLOR_HELP, rich_help_panel=_APPEARANCE
    ),
) -> None:
    """List the AI assistants Ferry can detect on this machine.

    Works without a terminal, so it is useful in a script. Exits non-zero when
    nothing is detected.
    """
    from ferry.cli.menu import scan

    capability = Capability.NO_COLOR if no_color else detect_capability()
    ui = UI(resolve_theme(theme, capability=capability), capability=capability)
    results = scan(ui)
    raise typer.Exit(0 if any(r.installed for _, r in results) else 1)


def _tool_callback(value: str) -> str:
    """Reject an unknown assistant name early, with the valid ones listed."""
    from ferry.adapters import REGISTRY

    name = value.strip().lower()
    if name not in REGISTRY:
        valid = ", ".join(REGISTRY)
        raise typer.BadParameter(f"unknown tool {value!r}. Choose one of: {valid}")
    return name


def _command_ui(theme: str | None, no_color: bool) -> UI:
    capability = Capability.NO_COLOR if no_color else detect_capability()
    return UI(resolve_theme(theme, capability=capability), capability=capability)


_PASSPHRASE_HELP = (
    "Passphrase for the sealed bundle. Prefer the environment variable: a flag "
    "is kept in your shell history."
)


@app.command("export", cls=FerryCommand)
def export_command(
    tool: str = typer.Option(
        ...,
        "--tool",
        "-t",
        metavar="TOOL",
        callback=_tool_callback,
        help=f"The assistant to export from: {_TOOL_NAMES}.",
    ),
    output: str = typer.Option(
        "",
        "--output",
        "-o",
        metavar="FOLDER",
        help="Folder to write the bundle into. Defaults to a new ferry-bundle-<time> folder here.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Add to a folder that already holds something; an interrupted export resumes "
        "this way. With --encrypt, also replaces an existing sealed file.",
    ),
    encrypt: bool = typer.Option(
        False,
        "--encrypt",
        help="Also seal the bundle into one encrypted .ferry file.",
        rich_help_panel=_SEALING,
    ),
    passphrase: str | None = typer.Option(
        None,
        "--passphrase",
        metavar="TEXT",
        envvar="FERRY_PASSPHRASE",
        help=_PASSPHRASE_HELP,
        rich_help_panel=_SEALING,
    ),
    replace: bool = typer.Option(
        False,
        "--replace",
        help="Delete the unencrypted folder once the sealed file is proven to open.",
        rich_help_panel=_SEALING,
    ),
    theme: str | None = typer.Option(
        None,
        "--theme",
        metavar="NAME",
        callback=_theme_callback,
        help=_THEME_HELP,
        rich_help_panel=_APPEARANCE,
    ),
    no_color: bool = typer.Option(
        False, "--no-color", help=_NO_COLOR_HELP, rich_help_panel=_APPEARANCE
    ),
) -> None:
    """Export an assistant's conversations into a bundle.

    Asks nothing when every answer is given as a flag, so it runs in a script.
    """
    from ferry.cli.commands import export_bundle

    raise typer.Exit(
        export_bundle(
            _command_ui(theme, no_color),
            tool=tool,
            output=output,
            force=force,
            encrypt=encrypt,
            passphrase=passphrase,
            replace=replace,
        )
    )


# Module-level because their values are lists: an Option built in the signature
# of a repeatable flag is the mutable-default pattern ruff's B008 exists to catch.
_CONVERSATION_OPTION = typer.Option(
    None,
    "--conversation",
    "-c",
    metavar="ID",
    help="Import only this conversation, by id. Repeat for more. Default: all of them.",
)
_PATH_REMAP_OPTION = typer.Option(
    None,
    "--path-remap",
    metavar="OLD=NEW",
    help="Read folders recorded under OLD as being under NEW. Repeatable.",
)


@app.command("import", cls=FerryCommand)
def import_command(
    bundle: str = typer.Option(
        ...,
        "--bundle",
        "-b",
        metavar="PATH",
        help="The bundle to import: a folder, or a sealed .ferry file.",
    ),
    tool: str = typer.Option(
        ...,
        "--tool",
        "-t",
        metavar="TOOL",
        callback=_tool_callback,
        help=f"The assistant to import into: {_TOOL_NAMES}.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show everything that would happen. Nothing is written."
    ),
    on_conflict: str = typer.Option(
        "skip",
        "--on-conflict",
        metavar="skip|rename|overwrite",
        help="When the assistant already has a conversation: keep its copy, keep both, or "
        "replace its copy after backing it up.",
    ),
    conversation: list[str] | None = _CONVERSATION_OPTION,
    path_remap: list[str] | None = _PATH_REMAP_OPTION,
    allow_cross_tool: bool = typer.Option(
        False,
        "--allow-cross-tool",
        help="Convert conversations that came from a different assistant. Refused without it.",
        rich_help_panel=_CROSSING,
    ),
    mode: str = typer.Option(
        "archive",
        "--mode",
        metavar="archive|continue",
        help="Keep the most detail, to read; or drop thinking and tool output, to carry on "
        "working in it.",
        rich_help_panel=_CROSSING,
    ),
    passphrase: str | None = typer.Option(
        None,
        "--passphrase",
        metavar="TEXT",
        envvar="FERRY_PASSPHRASE",
        help=_PASSPHRASE_HELP,
        rich_help_panel=_SEALING,
    ),
    theme: str | None = typer.Option(
        None,
        "--theme",
        metavar="NAME",
        callback=_theme_callback,
        help=_THEME_HELP,
        rich_help_panel=_APPEARANCE,
    ),
    no_color: bool = typer.Option(
        False, "--no-color", help=_NO_COLOR_HELP, rich_help_panel=_APPEARANCE
    ),
) -> None:
    """Import a bundle into an assistant.

    Backs up before it writes, keeps any conversation the assistant already
    has, and asks nothing when every answer is given as a flag.
    """
    from ferry.cli.commands import import_bundle

    if on_conflict not in ("skip", "rename", "overwrite"):
        raise typer.BadParameter(
            f"unknown choice {on_conflict!r}. Choose one of: skip, rename, overwrite",
            param_hint="--on-conflict",
        )
    if mode not in ("archive", "continue"):
        raise typer.BadParameter(
            f"unknown mode {mode!r}. Choose one of: archive, continue", param_hint="--mode"
        )

    raise typer.Exit(
        import_bundle(
            _command_ui(theme, no_color),
            bundle=bundle,
            tool=tool,
            on_conflict=cast("OnConflict", on_conflict),
            dry_run=dry_run,
            conversations=conversation or (),
            path_remap=path_remap or (),
            allow_cross_tool=allow_cross_tool,
            mode=cast("ConversionMode", mode),
            passphrase=passphrase,
        )
    )


@app.command("skill", cls=FerryCommand)
def skill_command(
    install: bool = typer.Option(
        False,
        "--install",
        help="Install it as a Claude Code skill, in ~/.claude/skills/ferry/.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="With --install: replace a different SKILL.md already there, after backing it up.",
    ),
    theme: str | None = typer.Option(
        None,
        "--theme",
        metavar="NAME",
        callback=_theme_callback,
        help=_THEME_HELP,
        rich_help_panel=_APPEARANCE,
    ),
    no_color: bool = typer.Option(
        False, "--no-color", help=_NO_COLOR_HELP, rich_help_panel=_APPEARANCE
    ),
) -> None:
    """Print the SKILL.md that teaches an AI assistant to use Ferry.

    Prints to standard output, so it pipes, or can be pasted into any
    assistant. --install puts it where Claude Code looks for skills.
    """
    from ferry.skill import skill_text

    if force and not install:
        raise typer.BadParameter("only means something with --install", param_hint="--force")
    if not install:
        try:
            typer.echo(skill_text(), nl=False)
        except (BrokenPipeError, OSError):
            # `ferry skill | head` closes the pipe early; that is not an error.
            return
        return

    from ferry.cli.commands import install_skill

    raise typer.Exit(install_skill(_command_ui(theme, no_color), force=force))


@app.command("compact", cls=FerryCommand)
def compact_command(
    bundle: str = typer.Argument(
        ..., metavar="BUNDLE", help="The bundle holding the conversation."
    ),
    conversation: str = typer.Option(
        "",
        "--conversation",
        "-c",
        metavar="ID",
        help="Which conversation, by id. Lists them when omitted.",
    ),
    shape: str = typer.Option(
        "handoff", "--shape", metavar="handoff|said|done", help="What the document keeps."
    ),
    length: str = typer.Option(
        "standard", "--length", metavar="brief|standard|full", help="How long it runs."
    ),
    out: str = typer.Option(
        "", "--out", metavar="FILE", help="Write to a file instead of standard output."
    ),
) -> None:
    """Compact one conversation in a bundle into a markdown document.

    Prints to standard output by default, so it pipes. Nothing is sent
    anywhere: the document is built on this machine from the bundle alone.

    A sealed bundle cannot be read here -- it needs a passphrase, and asking
    for one is what the interactive screen is for.
    """
    from pathlib import Path

    from ferry import __version__
    from ferry.compact import LENGTHS, SHAPES, compact
    from ferry.core import Bundle, BundleError

    if shape not in SHAPES:
        raise typer.BadParameter(f"unknown shape {shape!r}. Choose one of: {', '.join(SHAPES)}")
    if length not in LENGTHS:
        valid = ", ".join(sorted(LENGTHS))
        raise typer.BadParameter(f"unknown length {length!r}. Choose one of: {valid}")

    try:
        opened = Bundle.open(Path(bundle).expanduser())
    except (BundleError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc

    known = opened.list_conversations()
    if not conversation:
        # Listing rather than guessing. Picking "the first one" would quietly
        # produce a document about the wrong conversation, which is worse than
        # printing nothing.
        typer.echo("Which conversation? Pass --conversation with one of:", err=True)
        for found in known:
            typer.echo(f"  {found}", err=True)
        raise typer.Exit(2 if known else 1)

    wanted = next((found for found in known if str(found) == conversation), None)
    if wanted is None:
        typer.echo(f"{conversation} is not in this bundle.", err=True)
        raise typer.Exit(2)

    document = compact(
        opened.load_conversation(wanted), shape=shape, length=length, version=__version__
    )

    if not out:
        try:
            typer.echo(document, nl=False)
        except (BrokenPipeError, OSError):
            # `ferry compact ... | head` closes the pipe partway through, and a
            # command written to be piped should not answer that with a
            # traceback. Nothing has gone wrong: the reader stopped reading.
            return
        return

    target = Path(out).expanduser()
    if target.exists():
        typer.echo(f"There is already something at {target}.", err=True)
        raise typer.Exit(2)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(document, encoding="utf-8", newline="\n")
    except OSError as exc:
        typer.echo(f"Could not write it: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"Saved to {target}", err=True)
