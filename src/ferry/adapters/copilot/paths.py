"""Where VS Code keeps Copilot Chat data, and how it names the directories.

Two stores hold transcripts, and a conversation is in one or the other
depending on whether a folder was open when it started::

    <user>/workspaceStorage/<key>/chatSessions/<uuid>.jsonl   folder or workspace
    <user>/globalStorage/emptyWindowChatSessions/<uuid>.jsonl no folder open

``<key>`` is the part that had to be reverse-engineered. It is not a name, a
slug or an encoding of the path -- it is a digest, and for a folder the digest
covers **the folder's creation time as well as its path**, which is why no
amount of guessing from the path alone can produce it. The rule below is read
from VS Code's own ``main.js`` and verified against four real directories on
this machine; see ``PROGRESS.md`` §4.2.

Verified against VS Code 1.134.0 on Windows.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from uuid import UUID

__all__ = [
    "CHAT_IMAGES_DIR",
    "EMPTY_WINDOW_DIR",
    "USER_DIR_ENV",
    "chat_images_dir",
    "empty_window_dir",
    "folder_key",
    "global_storage",
    "session_files",
    "session_id_of",
    "user_dir",
    "vscode_fs_path",
    "workspace_file_key",
    "workspace_storage",
]

USER_DIR_ENV = "FERRY_VSCODE_USER_DIR"
"""Points Ferry at a different VS Code user directory.

**Ferry's own variable, not one VS Code reads.** VS Code chooses its user
directory with a ``--user-data-dir`` command-line flag, which is no use to a
program that is not launching it. Tests set this; so can anyone whose install
is somewhere unusual.
"""

EMPTY_WINDOW_DIR = "emptyWindowChatSessions"
"""Conversations started with no folder open, under ``globalStorage``."""

CHAT_IMAGES_DIR = "vscode-chat-images"
"""Images pasted into any chat.

Sits *beside* the workspace keys rather than inside one, so it is shared
across every workspace. An import must not assume it belongs to the
conversation that brought it.
"""


def user_dir(env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """VS Code's user directory, where both chat stores live."""
    environ = os.environ if env is None else env
    override = environ.get(USER_DIR_ENV)
    if override:
        return Path(override)
    if sys.platform == "win32":
        appdata = environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "Code" / "User"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User"
    return Path.home() / ".config" / "Code" / "User"


def workspace_storage(env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """The root holding one directory per workspace, named by its key."""
    return user_dir(env) / "workspaceStorage"


def global_storage(env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """The root holding cross-workspace state."""
    return user_dir(env) / "globalStorage"


def empty_window_dir(env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """Where conversations started without a folder open are kept."""
    return global_storage(env) / EMPTY_WINDOW_DIR


def chat_images_dir(env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """The shared store for images pasted into any chat."""
    return workspace_storage(env) / CHAT_IMAGES_DIR


def session_files(
    env: os._Environ[str] | dict[str, str] | None = None,
) -> list[tuple[str, Path]]:
    """Every chat transcript, as ``(workspace key or "", path)``.

    The key is empty for an empty-window conversation, which has no workspace
    to belong to. Sorted so an interrupted export resumes predictably rather
    than in whatever order the filesystem offers.
    """
    found: list[tuple[str, Path]] = []
    root = workspace_storage(env)
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            sessions = entry / "chatSessions"
            if sessions.is_dir():
                found.extend((entry.name, path) for path in sorted(sessions.glob("*.jsonl")))
    empty = empty_window_dir(env)
    if empty.is_dir():
        found.extend(("", path) for path in sorted(empty.glob("*.jsonl")))
    return found


def session_id_of(path: Path) -> UUID | None:
    """The conversation's id, or ``None`` if the filename is not one."""
    try:
        return UUID(path.stem)
    except ValueError:
        return None


def vscode_fs_path(path: Path) -> str:
    """A path spelled the way VS Code spells it in ``URI.fsPath``.

    Backslashes on Windows, and a **lowercase drive letter** -- VS Code
    normalises the drive when it parses the URI, and the digest is taken over
    that spelling. Feeding it ``C:`` where VS Code used ``c:`` produces a
    different, entirely plausible-looking directory name.
    """
    text = str(Path(path))
    if sys.platform == "win32" and len(text) > 1 and text[1] == ":":
        return text[0].lower() + text[1:]
    return text


def _identity(stat: os.stat_result) -> str | None:
    """The part of a folder's identity that is not its path, as VS Code sees it.

    ==========  ================================================
    Linux       ``st_ino`` -- the inode number
    macOS       birth time in milliseconds
    Windows     birth time in milliseconds, floored
    ==========  ================================================

    **``st_birthtime`` does not exist on Windows before Python 3.12**, and
    Ferry supports 3.11. There the creation time is ``st_ctime`` -- the same
    number under an older name, not the metadata-change time it means
    elsewhere. Without this fallback the whole derivation returned ``None`` on
    the oldest Python Ferry claims to run on, which CI caught and a 3.14
    machine could not.
    """
    if sys.platform == "linux":
        return str(stat.st_ino)

    birth = getattr(stat, "st_birthtime", None)
    if birth is None:
        if sys.platform != "win32":
            # On macOS st_ctime is the metadata-change time and would produce a
            # confidently wrong key. Better to decline, as VS Code does.
            return None
        birth = stat.st_ctime
    return str(int(birth * 1000))


def folder_key(folder: Path) -> str | None:
    """The ``workspaceStorage`` directory name VS Code uses for this folder.

    ``md5(fsPath + stamp)``, where the stamp is the folder's identity on disk
    -- see :func:`_identity`, which differs by platform.

    Using the wrong one does not raise. It produces a valid-looking key for a
    directory that does not exist, so an import would write a conversation
    somewhere VS Code will never look. That is why the branch lives in one
    function rather than being a formula repeated at call sites.

    Returns:
        The key, or ``None`` if the folder cannot be stat'd -- VS Code also
        declines to produce an id in that case rather than inventing one.
    """
    try:
        stat = folder.stat()
    except OSError:
        return None

    stamp = _identity(stat)
    if stamp is None:
        return None

    return hashlib.md5(  # noqa: S324 - matching VS Code's choice, not securing anything
        (vscode_fs_path(folder) + stamp).encode()
    ).hexdigest()


def workspace_file_key(workspace_file: Path) -> str:
    """The ``workspaceStorage`` directory name for a ``.code-workspace`` file.

    No timestamp here -- a workspace file is identified by its path alone,
    lowercased everywhere except Linux, where paths are case-sensitive.
    """
    text = vscode_fs_path(workspace_file)
    if sys.platform != "linux":
        text = text.lower()
    return hashlib.md5(text.encode()).hexdigest()  # noqa: S324 - matching VS Code
