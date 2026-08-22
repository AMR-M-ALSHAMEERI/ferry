"""Themes, icon sets, and terminal-capability fallback for Ferry's interface.

Four themes are defined (PLAN.md §5 M2):

- ``harbor``  — the default identity theme, teal and amber
- ``compass`` — the cooler identity theme, sky and indigo
- ``classic`` — the 8 standard ANSI colours only, inheriting the user's palette
- ``mono``    — no colour at all, ASCII markers, safe for log files and CI

``mono`` doubles as the automatic fallback: when the terminal cannot render
colour, or output is not a TTY, Ferry degrades rather than emitting escape
codes into a file.

No emoji appear anywhere, and no Nerd Font glyphs either — those need a patched
font and render as tofu boxes without one. The coloured themes use geometric
BMP Unicode; ``mono`` uses plain ASCII.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Final

__all__ = [
    "DEFAULT_THEME",
    "THEMES",
    "Capability",
    "IconSet",
    "Theme",
    "detect_capability",
    "resolve_theme",
    "supports_unicode",
]


class Capability(Enum):
    """How much presentation the output stream can actually carry.

    Ordered from richest to plainest. ``resolve_theme`` uses this ordering to
    decide how far a requested theme has to degrade.
    """

    COLOR = "color"
    """A TTY that can render colour."""

    NO_COLOR = "no_color"
    """A TTY, but colour is unavailable or the user opted out."""

    PLAIN = "plain"
    """Not a TTY at all — piped, redirected, or running in CI."""


@dataclass(frozen=True)
class IconSet:
    """The glyphs a theme uses for status and interaction.

    Kept separate from colour so ``mono`` can drop to ASCII while the coloured
    themes share one Unicode set.
    """

    success: str
    error: str
    warning: str
    info: str
    cursor: str
    selected: str
    unselected: str
    checked: str
    unchecked: str
    progress_fill: str
    progress_track: str
    ellipsis: str
    """Trailing mark for in-progress labels. ASCII under ``mono`` so piped
    output stays pure ASCII — a Unicode ellipsis is mangled by legacy Windows
    code pages when redirected to a file."""


UNICODE_ICONS: Final = IconSet(
    success="✔",
    error="✖",
    warning="▲",
    info="›",
    cursor="❯",
    selected="◉",
    unselected="○",
    checked="◼",
    unchecked="◻",
    progress_fill="━",
    progress_track="─",
    ellipsis="…",
)
"""Geometric BMP Unicode. No emoji presentation, no font dependency."""

ASCII_ICONS: Final = IconSet(
    success="[ok]",
    error="[--]",
    warning="[!]",
    info="[i]",
    cursor=">",
    selected="(*)",
    unselected="( )",
    checked="[x]",
    unchecked="[ ]",
    progress_fill="=",
    progress_track="-",
    ellipsis="...",
)
"""Pure ASCII, for log files and terminals that mangle Unicode."""

_UNICODE_PROBE: Final = "".join(getattr(UNICODE_ICONS, f.name) for f in dataclasses.fields(IconSet))
"""Every Unicode glyph at once, for testing whether a stream can encode them."""


def supports_unicode(stream: object | None = None) -> bool:
    """Whether the output stream can actually encode the Unicode icon set.

    Windows consoles frequently report an encoding like ``cp1252``, which has no
    mapping for these glyphs. Printing one there raises ``UnicodeEncodeError``
    and takes the whole program down — so this is a crash guard, not a
    cosmetic check.

    Args:
        stream: Stream to test. Defaults to ``sys.stdout``.

    Returns:
        ``True`` if every glyph can be encoded, ``False`` otherwise.
    """
    out = sys.stdout if stream is None else stream
    encoding = getattr(out, "encoding", None)
    if not encoding:
        return False
    try:
        _UNICODE_PROBE.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


@dataclass(frozen=True)
class Theme:
    """A named palette plus the icon set that goes with it.

    Colour fields are ``rich`` style strings. An empty string means "no styling"
    — that is how ``mono`` switches colour off without needing a separate code
    path at every call site.
    """

    name: str
    icons: IconSet
    primary: str
    accent: str
    success: str
    error: str
    warning: str
    dim: str
    text: str
    heading: str

    @property
    def uses_color(self) -> bool:
        """Whether this theme emits any styling at all."""
        return bool(self.primary)


HARBOR: Final = Theme(
    name="harbor",
    icons=UNICODE_ICONS,
    primary="#5eead4",
    accent="#f59e0b",
    success="#34d399",
    error="#f87171",
    warning="#f59e0b",
    dim="#64748b",
    text="#e2e8f0",
    heading="bold #5eead4",
)

COMPASS: Final = Theme(
    name="compass",
    icons=UNICODE_ICONS,
    primary="#7dd3fc",
    accent="#a5b4fc",
    success="#86efac",
    error="#fca5a5",
    warning="#fcd34d",
    dim="#6b7280",
    text="#e5e7eb",
    heading="bold #7dd3fc",
)

CLASSIC: Final = Theme(
    name="classic",
    icons=UNICODE_ICONS,
    primary="cyan",
    accent="blue",
    success="green",
    error="red",
    warning="yellow",
    dim="bright_black",
    text="default",
    heading="bold cyan",
)

MONO: Final = Theme(
    name="mono",
    icons=ASCII_ICONS,
    primary="",
    accent="",
    success="",
    error="",
    warning="",
    dim="",
    text="",
    heading="",
)

THEMES: Final[dict[str, Theme]] = {t.name: t for t in (HARBOR, COMPASS, CLASSIC, MONO)}

DEFAULT_THEME: Final = "harbor"


def detect_capability(
    *,
    stream: object | None = None,
    env: Mapping[str, str] | None = None,
) -> Capability:
    """Work out how much presentation the output stream can carry.

    Honours the ``NO_COLOR`` convention (https://no-color.org) and Ferry's own
    ``FERRY_NO_COLOR``. Both are treated as set-means-on regardless of value,
    which is what the convention specifies.

    Args:
        stream: The output stream to inspect. Defaults to ``sys.stdout``.
        env: Environment mapping to read. Defaults to ``os.environ``.

    Returns:
        The richest capability the stream can support.
    """
    environ = os.environ if env is None else env
    out = sys.stdout if stream is None else stream

    isatty = getattr(out, "isatty", None)
    if not callable(isatty) or not isatty():
        return Capability.PLAIN

    if "NO_COLOR" in environ or "FERRY_NO_COLOR" in environ:
        return Capability.NO_COLOR

    if environ.get("TERM") == "dumb":
        return Capability.NO_COLOR

    return Capability.COLOR


def resolve_theme(
    requested: str | None = None,
    *,
    capability: Capability | None = None,
    env: Mapping[str, str] | None = None,
    stream: object | None = None,
) -> Theme:
    """Pick the theme to use, degrading it to what the terminal can show.

    Selection order for the *requested* name, highest priority first: the
    ``requested`` argument (the ``--theme`` flag), then ``FERRY_THEME``, then
    :data:`DEFAULT_THEME`. A name that is not recognised falls back to the
    default rather than raising — a bad theme name should never stop someone
    migrating their conversations.

    The requested theme is then capped by ``capability``:

    - :attr:`Capability.COLOR` — used as requested
    - :attr:`Capability.NO_COLOR` — capped at ``mono``
    - :attr:`Capability.PLAIN` — capped at ``mono``

    Args:
        requested: Theme name from the CLI flag, or ``None``.
        capability: Detected capability. Detected automatically when ``None``.
        env: Environment mapping to read. Defaults to ``os.environ``.

    Returns:
        The theme that should actually be used.
    """
    environ = os.environ if env is None else env
    cap = detect_capability(env=environ) if capability is None else capability

    name = requested or environ.get("FERRY_THEME") or DEFAULT_THEME
    theme = THEMES.get(name.strip().lower(), THEMES[DEFAULT_THEME])

    if cap is not Capability.COLOR:
        return MONO

    if theme.icons is UNICODE_ICONS and not supports_unicode(stream):
        return dataclasses.replace(theme, icons=ASCII_ICONS)
    return theme
