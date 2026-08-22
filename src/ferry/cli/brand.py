"""Ferry's wordmark — the three lines shown when the tool starts.

Design "D — wordmark + wake", chosen 2026-08-21 (PROGRESS.md ledger #69):

    ⟢  F E R R Y   0.1.0
    ≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈≈
    carry your conversations across

The wake animates by drawing left to right — the ferry crossing. Everything
here has an ASCII twin, because a console reporting ``cp1252`` cannot encode
``⟢`` or ``≈`` and would crash rather than look wrong.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ferry import __version__
from ferry.cli.theme import ASCII_ICONS, BRAND_MARK, BRAND_WAKE, Theme

__all__ = ["WORDMARK_WIDTH", "Wordmark", "build_wordmark"]

TAGLINE = "carry your conversations across"
WORDMARK_WIDTH = 34
"""Width of the wake, chosen to sit just wider than the tagline."""

_ASCII_MARK = ">"
_ASCII_WAKE = "~"


@dataclass(frozen=True)
class Wordmark:
    """The pieces of the wordmark, resolved for one theme.

    Kept as data rather than pre-rendered text so the animation can redraw the
    wake at successive lengths without rebuilding everything else.
    """

    mark: str
    name: str
    version: str
    wake_char: str
    tagline: str
    width: int

    def wake(self, filled: int) -> tuple[str, str]:
        """Split the wake into its drawn and undrawn halves.

        Args:
            filled: How many characters have been drawn so far.

        Returns:
            ``(drawn, remaining)`` — the caller styles them differently so the
            leading edge of the wake reads as brighter than the trail.
        """
        filled = max(0, min(filled, self.width))
        return self.wake_char * filled, self.wake_char * (self.width - filled)


def build_wordmark(theme: Theme, *, version: str = __version__) -> Wordmark:
    """Assemble the wordmark for a theme, picking glyphs it can actually render."""
    ascii_only = theme.icons is ASCII_ICONS
    return Wordmark(
        mark=_ASCII_MARK if ascii_only else BRAND_MARK,
        name="F E R R Y",
        version=version,
        wake_char=_ASCII_WAKE if ascii_only else BRAND_WAKE,
        tagline=TAGLINE,
        width=WORDMARK_WIDTH,
    )


def render(ui: object, theme: Theme, *, animate: bool = True) -> None:
    """Print the wordmark, animating the wake when the terminal allows it.

    Args:
        ui: The :class:`~ferry.cli.ui.UI` doing the printing.
        theme: Active theme.
        animate: Whether to draw the wake progressively. Suppressed anyway
            whenever the theme carries no colour or output is not a terminal.
    """
    from ferry.cli.ui import UI

    assert isinstance(ui, UI)
    wm = build_wordmark(theme)
    console = ui.console

    def line1() -> str:
        if not theme.uses_color:
            return f"  {wm.mark}  {wm.name}   {wm.version}"
        return (
            f"  [ferry.accent]{wm.mark}[/ferry.accent]  "
            f"[ferry.heading]{wm.name}[/ferry.heading]   "
            f"[ferry.dim]{wm.version}[/ferry.dim]"
        )

    def wake_line(filled: int) -> str:
        drawn, rest = wm.wake(filled)
        if not theme.uses_color:
            return f"  {drawn}{rest}"
        return f"  [ferry.primary]{drawn}[/ferry.primary][ferry.dim]{rest}[/ferry.dim]"

    def tagline() -> str:
        if not theme.uses_color:
            return f"  {wm.tagline}"
        return f"  [ferry.dim]{wm.tagline}[/ferry.dim]"

    console.print()
    console.print(line1())

    can_animate = animate and theme.uses_color and ui.interactive
    if not can_animate:
        console.print(wake_line(wm.width))
        console.print(tagline())
        console.print()
        return

    step = max(1, wm.width // 14)
    for filled in range(0, wm.width + 1, step):
        console.print(wake_line(filled), end="\r")
        time.sleep(0.012)
    console.print(wake_line(wm.width))
    console.print(tagline())
    console.print()
