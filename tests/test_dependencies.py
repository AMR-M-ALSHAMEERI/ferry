"""Every library Ferry imports is a library Ferry asks for.

Written after all twelve CI legs went red at once. Dropping ``questionary``
also dropped ``prompt_toolkit``, which nothing declared and which three CLI
modules import directly: it had been arriving transitively under questionary
for eleven days. The local virtualenv still had it, so the whole suite, ruff
and mypy stayed green here while every clean environment failed instantly.

**An undeclared dependency is invisible in the environment that already has
it.** That is the whole failure, and it is not something a test of behaviour
can catch, because the behaviour is correct. So this checks the packaging
instead: what the source imports, against what the project asks to be
installed.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src"

#: Distributions whose import name differs from the name on PyPI. Kept as an
#: explicit map rather than guessed: a wrong guess here would silently excuse a
#: missing dependency, which is the thing being tested for.
IMPORT_NAMES = {
    "prompt_toolkit": "prompt_toolkit",
}


def declared() -> set[str]:
    """The runtime dependencies, by import name, from ``pyproject.toml``."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names: set[str] = set()
    for requirement in data["project"]["dependencies"]:
        # "name>=1.2" or "name[extra]>=1.2" down to just the name.
        name = requirement.split(";")[0].strip()
        for separator in ("[", ">", "<", "=", "!", "~", " "):
            name = name.split(separator)[0]
        canonical = name.strip().lower().replace("-", "_")
        names.add(IMPORT_NAMES.get(canonical, canonical))
    return names


def imported() -> dict[str, set[str]]:
    """Top-level third-party modules imported anywhere under ``src``."""
    standard = set(sys.stdlib_module_names)
    found: dict[str, set[str]] = {}
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                # `level == 0` only: a relative import is Ferry's own code.
                modules = [node.module]
            else:
                continue
            for module in modules:
                top = module.split(".")[0]
                if top in standard or top == "ferry" or top.startswith("_"):
                    continue
                found.setdefault(top, set()).add(str(path.relative_to(ROOT)))
    return found


def test_every_third_party_import_is_declared() -> None:
    """The check that would have saved twelve CI legs.

    Walks every import statement rather than the top of each file, so a lazy
    import inside a function counts too: it fails just as hard at runtime, and
    later, in front of someone using the tool.
    """
    missing = {
        module: sorted(files) for module, files in imported().items() if module not in declared()
    }

    assert not missing, "imported but not in pyproject dependencies: " + "; ".join(
        f"{module} ({', '.join(files)})" for module, files in sorted(missing.items())
    )


def test_the_declared_names_can_actually_be_read() -> None:
    """A guard on the parsing above, not on the project.

    If the requirement strings ever stop being parsed correctly, every name
    becomes unrecognisable and the real test above passes by accident.
    """
    names = declared()

    assert "prompt_toolkit" in names
    assert "pydantic" in names
    assert not any(character in name for name in names for character in ">=<[ ")
