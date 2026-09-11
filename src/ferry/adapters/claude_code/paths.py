"""Where Claude Code keeps its data, and how it names project directories.

The project directory name is the one thing in this adapter that had to be
exactly right: it is derived from the working directory by a lossy substitution,
so getting it wrong misfiles every conversation without ever raising an error.

The rule below is not a guess. It was read out of the shipped Claude Code
binary (2.1.237) and then confirmed empirically -- see ``docs/FORMATS.md``.
Both sources agree:

    dirName(cwd) = cwd.replace(/[^a-zA-Z0-9]/g, "-")            when short enough
                 = mangled[:200] + "-" + base36(int32Hash(cwd))  otherwise

Because the substitution is lossy (``a.b`` and ``a-b`` and ``a_b`` all become
``a-b``), it can never be reversed. Import must therefore derive the name from
the *target* machine's working directory and never attempt to decode the old
one. That is worth repeating next to the code that would be tempted to try.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

from ferry.adapters.pathutil import basename

__all__ = [
    "CONFIG_DIR_ENV",
    "MAX_PROJECT_DIR_NAME",
    "PROJECT_DIR_NAME_ENV",
    "basename",
    "config_root",
    "mangle",
    "project_dir",
    "project_dir_name",
    "projects_dir",
    "session_files",
    "sidecar_dir",
]

CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
"""Overrides ``~/.claude`` wholesale."""

PROJECT_DIR_NAME_ENV = "CLAUDE_CODE_PROJECT_DIR_NAME"
"""Pins the project directory name, bypassing the substitution entirely.

Only honoured when :data:`CONFIG_DIR_ENV` is also set -- that is the guard the
real implementation applies, and copying it matters: an adapter that honoured
the override on its own would look in a directory Claude Code never writes to.
"""

MAX_PROJECT_DIR_NAME = 200
"""Length at which the name is truncated and a hash suffix appended."""

_VALID_OVERRIDE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RESERVED_NAME = re.compile(r"^(?:con|prn|aux|nul|com[0-9]|lpt[0-9])$", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]")

_INT32_MIN = -(2**31)
_UINT32 = 2**32


def _int32_hash(text: str) -> int:
    """The 32-bit string hash Claude Code appends to over-long directory names.

    ``h = h * 31 + code`` with signed 32-bit wraparound -- the classic Java
    ``String.hashCode``, which is what the shipped implementation computes as
    ``(h << 5) - h + charCodeAt(i) | 0``.

    Iterates UTF-16 code units rather than Python characters, because
    JavaScript's ``charCodeAt`` sees a surrogate pair where Python sees one
    astral character. A path containing an emoji would otherwise hash
    differently here than in the tool.
    """
    units = text.encode("utf-16-le")
    h = 0
    for index in range(0, len(units), 2):
        code = units[index] | (units[index + 1] << 8)
        h = (h << 5) - h + code
        h = ((h - _INT32_MIN) % _UINT32) + _INT32_MIN
    return h


def _base36(value: int) -> str:
    """``Number.prototype.toString(36)`` for a non-negative integer."""
    if value == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out: list[str] = []
    while value:
        value, remainder = divmod(value, 36)
        out.append(digits[remainder])
    return "".join(reversed(out))


def mangle(path_text: str) -> str:
    r"""Turn an absolute working directory into its project directory name.

    Args:
        path_text: The working directory exactly as Claude Code saw it --
            native separators, original drive-letter case. ``C:\Users\a`` and
            ``C:/Users/a`` mangle identically, but only because ``\`` and ``/``
            are both non-alphanumeric, not because the path was normalised.

    Returns:
        The directory name under :func:`projects_dir`.
    """
    mangled = _NON_ALNUM.sub("-", path_text)
    if len(mangled) <= MAX_PROJECT_DIR_NAME:
        return mangled
    suffix = _base36(abs(_int32_hash(path_text)))
    return f"{mangled[:MAX_PROJECT_DIR_NAME]}-{suffix}"


def config_root(env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """The ``.claude`` directory: ``$CLAUDE_CONFIG_DIR`` or ``~/.claude``.

    NFC-normalised, matching the tool. On macOS the filesystem hands back
    decomposed names, so a path with an accented directory would otherwise not
    compare equal to the one Claude Code recorded.
    """
    environ = os.environ if env is None else env
    override = environ.get(CONFIG_DIR_ENV)
    root = Path(override) if override else Path.home() / ".claude"
    return Path(unicodedata.normalize("NFC", str(root)))


def projects_dir(env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """The directory holding one subdirectory per project."""
    return config_root(env) / "projects"


def project_dir_name(cwd: str | Path, env: os._Environ[str] | dict[str, str] | None = None) -> str:
    """The project directory name Claude Code would use for ``cwd``.

    Honours :data:`PROJECT_DIR_NAME_ENV` under the same conditions the tool
    does: the config dir must also be overridden, and the name must be a short
    safe token that is not a reserved Windows device name.
    """
    environ = os.environ if env is None else env
    if environ.get(CONFIG_DIR_ENV):
        override = environ.get(PROJECT_DIR_NAME_ENV)
        if override and _VALID_OVERRIDE.match(override) and not _RESERVED_NAME.match(override):
            return override
    return mangle(str(cwd))


def project_dir(cwd: str | Path, env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """Full path to the project directory for ``cwd``."""
    return projects_dir(env) / project_dir_name(cwd, env)


def session_files(project: Path) -> list[Path]:
    """Every session transcript in one project directory, sorted by name."""
    if not project.is_dir():
        return []
    return sorted(p for p in project.glob("*.jsonl") if p.is_file())


def sidecar_dir(session: Path) -> Path:
    """Where a session's spilled tool outputs live.

    Claude Code writes large tool results to
    ``<project>/<session-uuid>/tool-results/*.txt`` and leaves an absolute
    reference to them in the transcript. They are part of the conversation, so
    an export that leaves them behind produces dangling references on import.
    """
    return session.parent / session.stem / "tool-results"
