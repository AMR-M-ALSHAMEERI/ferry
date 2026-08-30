r"""Whether Claude Code will actually open a folder Ferry has written into.

Writing the transcript is not the whole job. Claude Code keeps a map of every
working directory it has been told to trust in ``~/.claude.json``, and a session
whose ``cwd`` is missing from that map is **listed by ``--resume`` and refuses
to open**:

    That session's folder isn't trusted yet.
    Start a session in that folder first, then try again.

This is the same shape of defect as Copilot's chat index, and it was found the
same way: by a person trying to open the thing Ferry said it had imported. A
conversation Ferry writes perfectly and the target will not open has not been
imported, whatever the summary said. Ferry cannot resolve it silently, because
trusting a folder is a security decision about running whatever ``CLAUDE.md``
and hooks live there. So Ferry checks, and says so, and leaves the decision
where it belongs.

**Keys are compared exactly, and that is a measured decision, not laziness.**
The map on the machine this was found on held two entries for one folder::

    'C:\\Users\\Dell\\Desktop\\Ferry' -> trusted
    'C:/Users/Dell/Desktop/Ferry'     -> not trusted

The same directory, spelled two ways, with two different answers. Matching
case-insensitively and separator-insensitively would therefore have reported
"trusted" for a spelling Claude Code considers untrusted, which is the one
wrong answer this module must never give: it would suppress the warning and
leave the person to discover the refusal at resume time, which is exactly the
situation it exists to prevent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ferry.adapters.claude_code.paths import CONFIG_DIR_ENV, config_root

__all__ = ["CONFIG_FILE_NAME", "TRUST_KEY", "advice", "config_file", "is_trusted"]

CONFIG_FILE_NAME = ".claude.json"
"""Claude Code's global settings file, holding the per-project trust map."""

TRUST_KEY = "hasTrustDialogAccepted"
"""The flag inside ``projects[<cwd>]`` that decides whether a folder opens."""


def config_file(env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """Where the trust map lives.

    Without an override this is ``~/.claude.json``, which is confirmed: it is
    the file read on the machine the refusal was found on, and the one the
    adapter's ``detect`` already looks for.

    Under ``CLAUDE_CONFIG_DIR`` it is that directory's own copy, **with no
    fallback to the home one**. Falling back looked harmless and is not: an
    override exists precisely to keep a run away from the real configuration,
    so reading the person's actual trust map when the override's copy is absent
    would let a test, or a run pointed at a spare config, answer from a file it
    was told not to touch. A missing file is "cannot tell" instead.
    """
    if (env if env is not None else os.environ).get(CONFIG_DIR_ENV):
        return config_root(env) / CONFIG_FILE_NAME
    return Path.home() / CONFIG_FILE_NAME


def is_trusted(
    cwd: str | Path, env: os._Environ[str] | dict[str, str] | None = None
) -> bool | None:
    """Say whether Claude Code will open sessions filed under ``cwd``.

    Args:
        cwd: The working directory exactly as it was written into the
            transcript. Spelling matters; see the module docstring.

    Returns:
        ``True`` if the folder is trusted, ``False`` if it is known not to be,
        and ``None`` if the answer cannot be read. The third case is kept
        separate on purpose: "no trust map here" and "this folder is untrusted"
        call for different things to be said, and collapsing them would have
        Ferry warn about a refusal that may never come.
    """
    try:
        path = config_file(env)
        if not path.is_file():
            return None
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError):
        return None
    projects = settings.get("projects")
    if not isinstance(projects, dict):
        return None
    entry = projects.get(str(cwd))
    if not isinstance(entry, dict):
        return False
    return entry.get(TRUST_KEY) is True


def advice(cwd: str | Path) -> str:
    """What to tell someone whose imported conversation will not open.

    Phrased as the one action that fixes it. A warning that describes a state
    without naming the remedy sends the person back to the tool that just
    refused them.
    """
    return (
        f"Claude Code will not open a conversation in a folder it has not been told to "
        f"trust, and {cwd} is not in its list yet. Start Claude Code once in that folder "
        f"and accept the prompt; after that this conversation will resume normally."
    )
