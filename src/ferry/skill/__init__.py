"""The ``SKILL.md`` that teaches an AI assistant to drive Ferry (PLAN.md §5 M8).

The file ships inside the package so an installed Ferry can hand it over:
``ferry skill`` prints it, ``ferry skill --install`` puts it where Claude Code
looks for skills. A copy also sits at the repository root for anyone reading
the source; a test keeps the two byte for byte the same.
"""

from __future__ import annotations

from importlib.resources import files
from typing import Final

__all__ = ["SKILL_NAME", "skill_text"]

SKILL_NAME: Final = "ferry"
"""The skill's name, and the folder Claude Code expects it in."""


def skill_text() -> str:
    """The contents of the packaged ``SKILL.md``."""
    return files(__package__).joinpath("SKILL.md").read_text(encoding="utf-8")
