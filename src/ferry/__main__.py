"""Allow ``python -m ferry`` as well as the installed ``ferry`` command."""

from ferry.cli import app

if __name__ == "__main__":
    app()
