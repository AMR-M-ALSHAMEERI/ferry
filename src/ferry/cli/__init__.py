"""Ferry CLI entry point. Interactive menu is built in M2 — this is a skeleton stub."""

import typer

from ferry import __version__

app = typer.Typer(
    name="ferry",
    help="Back up and migrate your local AI assistant conversation history.",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"ferry {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Ferry — back up and migrate your local AI assistant conversation history."""
