"""Smoke test: the package imports and the CLI app exists."""

import ferry
from ferry.cli import app


def test_package_has_version() -> None:
    """The package and the project file must agree.

    They are two separate literals, and a release that bumps one and not the
    other ships a wheel whose ``--version`` contradicts its own metadata.
    """
    import re
    from pathlib import Path

    assert re.fullmatch(r"\d+\.\d+\.\d+", ferry.__version__), ferry.__version__

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    declared = re.search(r'^version = "([^"]+)"', pyproject.read_text(encoding="utf-8"), re.M)
    assert declared is not None, "pyproject.toml declares no version"
    assert declared.group(1) == ferry.__version__


def test_cli_app_importable() -> None:
    assert app is not None
