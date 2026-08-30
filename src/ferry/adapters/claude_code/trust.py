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
imported, whatever the summary said. Ferry does not resolve it silently,
because trusting a folder is a security decision about running whatever
``CLAUDE.md`` and hooks live there. It is
offered on screen instead, for folders the person picked themselves, and
:func:`grant` runs only when they say so. The library default is **off**: a
programmatic import writes nothing into another tool's configuration unless
asked, and only the interactive screen puts the cursor on "yes".

Why it is offered at all rather than left as a warning: Ferry's main job is
restoring a backup onto a **new machine**, where every folder is untrusted. A
person restoring forty conversations across twelve project folders would meet
the refusal twelve times and go and fix it twelve times by hand.

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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ferry.adapters.claude_code.paths import CONFIG_DIR_ENV, config_root
from ferry.core import back_up

__all__ = [
    "CONFIG_FILE_NAME",
    "TRUST_KEY",
    "Granted",
    "TrustError",
    "advice",
    "config_file",
    "grant",
    "is_trusted",
]

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


# --------------------------------------------------------------------------
# granting it
# --------------------------------------------------------------------------


class TrustError(RuntimeError):
    """Trust could not be granted, and nothing was changed."""


@dataclass(frozen=True)
class Granted:
    """What :func:`grant` did.

    Args:
        folders: The directories now trusted, in the order asked for.
        backup: Where the previous settings file was copied before the write.
    """

    folders: tuple[str, ...]
    backup: Path


def grant(
    folders: Sequence[str],
    env: os._Environ[str] | dict[str, str] | None = None,
    *,
    backup_root: Path | None = None,
) -> Granted:
    """Tell Claude Code it may open ``folders``, or change nothing at all.

    This writes into a file another program owns and may have open, so it is
    done the careful way and in this order:

    1. **Copy it aside first.** A settings file is not a conversation; losing
       it loses every project's tool permissions and MCP servers along with the
       trust flags. If the copy fails, nothing is written.
    2. **Write a temporary file and rename it over the original.** A rename is
       atomic on both filesystems Ferry targets, so a crash or a full disk
       leaves the old file intact rather than a half-written one. Writing in
       place is what would produce an unreadable config.
    3. **Read it back and check.** The whole point of the write is that Claude
       Code will now open these folders, and the only evidence for that is the
       file saying so afterwards.

    Every other key is preserved: the file is loaded, one flag is set per
    folder, and everything else is written back as it was found. An existing
    entry keeps its ``allowedTools`` and MCP settings.

    **What this cannot do** is take a lock. Claude Code may be running and may
    rewrite the file from its own copy in memory, which would silently undo
    this. There is no advertised lock to take, so the honest mitigation is the
    backup and the read-back, not a claim of safety that is not there.

    Args:
        folders: Working directories, spelled exactly as they were written into
            the transcripts. See the module docstring on why spelling matters.

    Returns:
        What was granted and where the previous file was saved.

    Raises:
        TrustError: If there is no settings file, it cannot be read, the write
            fails, or the read-back does not show the folders as trusted. In
            every case the original file is untouched or restorable from the
            backup named in the message.
    """
    wanted = tuple(dict.fromkeys(str(folder) for folder in folders))
    if not wanted:
        raise TrustError("no folders to trust")

    path = config_file(env)
    if not path.is_file():
        # Refused rather than created. Ferry knows one key of this file's
        # schema; inventing the rest of a config Claude Code has never written
        # is how you produce a file that parses and means nothing.
        raise TrustError(f"no Claude Code settings file at {path}")
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TrustError(f"cannot read {path}: {exc}") from exc
    if not isinstance(settings, dict):
        raise TrustError(f"{path} is not the settings file Ferry expects")

    try:
        saved = back_up(path, "claude-code", root=backup_root)
    except OSError as exc:
        raise TrustError(f"could not back up {path}, so it was left alone: {exc}") from exc

    projects = settings.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    for folder in wanted:
        entry = projects.get(folder)
        projects[folder] = (
            {**entry, TRUST_KEY: True} if isinstance(entry, dict) else {TRUST_KEY: True}
        )
    settings["projects"] = projects

    temporary = path.with_suffix(path.suffix + ".ferry-tmp")
    try:
        temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise TrustError(f"could not write {path}; the copy at {saved} is intact: {exc}") from exc

    missed = [folder for folder in wanted if is_trusted(folder, env) is not True]
    if missed:
        raise TrustError(
            f"{path} was written but does not show {missed[0]} as trusted; "
            f"the previous file is at {saved}"
        )
    return Granted(folders=wanted, backup=saved)
