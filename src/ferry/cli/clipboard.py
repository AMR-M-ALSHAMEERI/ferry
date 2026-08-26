"""Putting text on the system clipboard, without taking a dependency for it.

The plan originally reached for ``pyperclip``. It is not used, and the reason
is worth stating because "just add the library" is usually the right answer.

Under the hood ``pyperclip`` shells out to the operating system's own clipboard
command -- ``clip`` on Windows, ``pbcopy`` on macOS, ``xclip`` or ``xsel`` on
Linux -- and on Linux it still fails when none of those is installed. So taking
the dependency does not remove the failure case Ferry has to handle anyway, and
costs about the same amount of code either way. Doing it here keeps Compact's
promise that it adds **nothing** to Ferry's install, which is a materially
stronger thing to be able to say than "one small dependency".

**A missing clipboard is not an error.** On a server, over SSH, in a container,
there is nothing to copy to. The caller asks :func:`available` first and simply
does not offer the option -- never a crash, never a stack trace, never a
apologetic message about a thing the person did not ask for.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

__all__ = ["available", "copy"]

_COMMANDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "win32": (("clip",),),
    "darwin": (("pbcopy",),),
}

_LINUX = (
    ("wl-copy",),
    ("xclip", "-selection", "clipboard"),
    ("xsel", "--clipboard", "--input"),
)
"""Wayland first, then X11. In that order because a Wayland session often still
has ``xclip`` present through XWayland, where it may or may not reach the
clipboard the user can actually paste from."""

_TIMEOUT = 5


def _candidates() -> tuple[tuple[str, ...], ...]:
    return _COMMANDS.get(sys.platform, _LINUX)


def _command() -> tuple[str, ...] | None:
    """The first clipboard command this machine actually has."""
    for candidate in _candidates():
        if shutil.which(candidate[0]):
            return candidate
    return None


def available() -> bool:
    """Is there anything on this machine to copy to?"""
    return _command() is not None


def copy(text: str) -> bool:
    """Put ``text`` on the clipboard.

    Returns:
        ``True`` when the clipboard command ran and accepted the text. ``False``
        for every other outcome -- no command, a non-zero exit, a hang. The
        caller says "copied" or offers to save a file instead; neither case is
        worth an exception, because the document is still on the screen either
        way.
    """
    command = _command()
    if command is None:
        return False
    try:
        finished = subprocess.run(  # noqa: S603 - a fixed command, text on stdin
            command,
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return finished.returncode == 0
