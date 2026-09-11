"""Smoke test: the package imports and the CLI app exists."""

import ferry
from ferry.cli import app


def test_package_has_version() -> None:
    assert ferry.__version__ == "0.1.0"


def test_cli_app_importable() -> None:
    assert app is not None
