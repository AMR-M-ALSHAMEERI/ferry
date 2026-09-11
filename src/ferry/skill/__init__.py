"""The ``SKILL.md`` that teaches an AI assistant to drive Ferry (PLAN.md §5 M8).

The file ships inside the package so an installed Ferry can hand it over:
``ferry skill`` prints it, ``ferry skill --install`` puts it where each
assistant looks for a person's own skills. A copy also sits at the repository
root for anyone reading the source; a test keeps the two byte for byte the same.
"""

from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path
from typing import Final

__all__ = [
    "COPILOT_ALSO_READS",
    "SKILLS_HOME_ENV",
    "SKILL_NAME",
    "skill_destinations",
    "skill_text",
]

SKILL_NAME: Final = "ferry"
"""The skill's name, and the folder every assistant expects it in."""

SKILLS_HOME_ENV: Final = "FERRY_SKILLS_HOME"
"""Moves the home folder that the Codex and Copilot skill folders sit under.

Ferry's own variable, and it exists for the test suite. Those two tools keep a
person's skills directly under the home folder, with no variable of their own
to point elsewhere, so without this the tests would install into the
developer's real ``~/.agents`` and ``~/.copilot``.
"""

COPILOT_ALSO_READS: Final = ("claude-code", "codex")
"""Assistants whose skill folders GitHub Copilot in VS Code reads as well as its own.

VS Code's documentation lists ``~/.copilot/skills``, ``~/.claude/skills`` and
``~/.agents/skills``. When Ferry installs for either of these, Copilot already
has the skill, and a copy of its own would only risk listing Ferry twice.
"""


def skill_text() -> str:
    """The contents of the packaged ``SKILL.md``."""
    return files(__package__).joinpath("SKILL.md").read_text(encoding="utf-8")


def skill_destinations(env: dict[str, str] | None = None) -> dict[str, Path]:
    """Where each assistant looks for a person's own skill named ``ferry``.

    Every location is the one the tool's own documentation gives, checked on
    2026-09-12: Claude Code reads ``~/.claude/skills``, OpenAI Codex
    ``~/.agents/skills``, GitHub Copilot in VS Code ``~/.copilot/skills`` (and
    the other two), and Antigravity ``~/.gemini/config/skills``. Claude Code's
    and Antigravity's follow the same variables Ferry already uses to find
    those tools, so a store moved for one moves for the other.
    """
    from ferry.adapters.antigravity.paths import skills_dir as antigravity_skills
    from ferry.adapters.claude_code.paths import config_root

    environ: dict[str, str] | os._Environ[str] = os.environ if env is None else env
    override = environ.get(SKILLS_HOME_ENV)
    home = Path(override) if override else Path.home()
    return {
        "claude-code": config_root(env) / "skills" / SKILL_NAME / "SKILL.md",
        "codex": home / ".agents" / "skills" / SKILL_NAME / "SKILL.md",
        "copilot": home / ".copilot" / "skills" / SKILL_NAME / "SKILL.md",
        "antigravity": antigravity_skills(env) / SKILL_NAME / "SKILL.md",
    }
