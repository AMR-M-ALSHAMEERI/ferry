"""Rewriting the absolute paths a conversation was recorded with.

A conversation carries the absolute path of the folder it happened in, and on
the machine it is restored to that path is usually wrong. For the other three
tools the paths sit in JSON and a remap is a string substitution. Here they are
inside protobuf blobs at fourteen or more field paths, which is why
:mod:`ferry.adapters.antigravity.wire` exists -- this module decides *what* a
path becomes, and that module makes the change without disturbing anything
else.

**Antigravity spells the same path four different ways**, and a remapper that
handles three of them leaves a conversation half-migrated -- which is worse
than not migrating it, because it still opens::

    C:\\Users\\Dell\\Project              plain, backslashes
    C:/Users/Dell/Project                plain, forward slashes
    file:///c:/Users/Dell/Project        URI, lowercase drive
    file:///c%3A%5CUsers%5CDell%5CProject  URI, percent-encoded, backslashes

The plan records the first three. The fourth was found in
``~/.gemini/config/projects/*.json``, where every ``folderUri`` uses it.

Matching also has to be case-insensitive for a Windows source, because the
drive letter is written both ways in the same database. That makes a remap
**many-to-one and not reversible**, which is correct behaviour and the reason
the round-trip tests use an exact-case rule instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

__all__ = ["PathRemapper", "spellings"]

_WINDOWS_ROOT = re.compile(r"^[A-Za-z]:[\\/]")

_BOUNDARY = r"(?![A-Za-z0-9_.\-])"
"""What must follow a matched prefix.

Without it ``C:\\Users\\Dell`` matches inside ``C:\\Users\\Dellinger`` and
renames a directory nobody asked about. A prefix may be followed by a
separator, a quote, a bracket, whitespace or the end of the string -- anything
except a character that would continue the name.
"""


def spellings(prefix: str) -> list[str]:
    """Every way Antigravity writes ``prefix``, longest first.

    Longest first matters: ``file:///c:/Users/Dell`` contains
    ``c:/Users/Dell``, so replacing the shorter form first would leave the URI
    prefix stranded in front of an already-substituted path.
    """
    plain = prefix.rstrip("\\/")
    if not plain:
        return []

    backslashes = plain.replace("/", "\\")
    forwards = plain.replace("\\", "/")

    if _WINDOWS_ROOT.match(plain):
        drive, rest = forwards[0], forwards[2:]
        uri = f"file:///{drive.lower()}:{rest}"
        # Lowercase drive in both URI forms, which is how Antigravity
        # writes them; the plain forms keep whatever case the caller gave.
        encoded = "file:///" + quote(drive.lower() + backslashes[1:], safe="")
        candidates = [encoded, uri, backslashes, forwards]
    else:
        candidates = [f"file://{forwards}", forwards]

    ordered: list[str] = []
    for candidate in sorted(dict.fromkeys(candidates), key=len, reverse=True):
        ordered.append(candidate)
    return ordered


@dataclass(frozen=True)
class PathRemapper:
    """Applies a set of ``(old prefix, new prefix)`` rules to any text.

    Rules are applied in the order given and the first that matches a given
    spelling wins, matching :class:`~ferry.adapters.base.ImportOptions`. A path
    no rule matches is left exactly as it was; an import with no rules changes
    nothing, and the blob it came from is written back as the bytes that were
    read.
    """

    rules: tuple[tuple[str, str], ...] = ()
    case_insensitive: bool = True
    """Whether a Windows-style source prefix matches regardless of case.

    On by default because Antigravity itself is inconsistent -- the same
    database holds ``C:\\Users`` and ``c:/Users`` for one directory. Turned off
    by the round-trip tests, which need an edit they can reverse.
    """

    def __post_init__(self) -> None:
        compiled: list[tuple[re.Pattern[str], str]] = []
        for old, new in self.rules:
            was, becomes = spellings(old), spellings(new)
            if not was or len(was) != len(becomes):
                # Different shapes -- a Windows prefix mapped onto a POSIX one,
                # which has no URI-encoded counterpart to pair with. Fall back
                # to the spellings they share rather than guessing at a pairing.
                was, becomes = was[-2:], becomes[-2:]
                if len(was) != len(becomes):
                    continue
            insensitive = self.case_insensitive and bool(_WINDOWS_ROOT.match(old))
            flags = re.IGNORECASE if insensitive else 0
            for pattern, replacement in zip(was, becomes, strict=True):
                compiled.append((re.compile(re.escape(pattern) + _BOUNDARY, flags), replacement))
        object.__setattr__(self, "_compiled", tuple(compiled))

    @property
    def active(self) -> bool:
        """Whether this remapper can change anything at all."""
        return bool(getattr(self, "_compiled", ()))

    def text(self, value: str) -> str:
        """``value`` with every matching prefix replaced."""
        for pattern, replacement in getattr(self, "_compiled", ()):
            if pattern.search(value):
                value = pattern.sub(replacement.replace("\\", "\\\\"), value)
        return value

    def field(self, _path: tuple[int, ...], value: str) -> str:
        """A :data:`~ferry.adapters.antigravity.wire.Rewriter` over any field.

        The field path is ignored deliberately. Restricting the rewrite to the
        fourteen paths the probe found would mean any field it did not see --
        a tool Ferry has no example of, a version that adds one -- keeps a path
        pointing at a directory that does not exist on this machine. Matching
        on the text is what makes the set of paths not need to be complete.
        """
        return self.text(value)
