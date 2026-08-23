"""Path handling shared by every adapter.

Ferry reads paths that another machine wrote. That is not an edge case, it is
the product: a bundle's paths come from the source machine and are read on the
target, and the two are frequently different operating systems.

``pathlib`` is the wrong tool for that job and wrong in a way that only shows
up on the other platform, which is how it reached CI green on Windows and red
on Linux and macOS during M3.
"""

from __future__ import annotations

__all__ = ["basename"]

_SEPARATORS = "/" + chr(92)


def basename(path_text: str) -> str:
    """The last segment of a path **recorded on any operating system**.

    ``Path(...).name`` splits using the separators of the machine it is running
    on, so a Windows path handed to a Linux interpreter has no separators at all
    and the "last segment" comes back as the entire string. Both separators are
    always significant here, whichever host is doing the reading.

    A trailing separator is ignored, and a path that is nothing but separators
    has no last segment, so the whole string comes back rather than an empty one.
    """
    trimmed = path_text.rstrip(_SEPARATORS)
    if not trimmed:
        return path_text
    cut = max(trimmed.rfind("/"), trimmed.rfind(chr(92)))
    return trimmed[cut + 1 :] if cut >= 0 else trimmed
