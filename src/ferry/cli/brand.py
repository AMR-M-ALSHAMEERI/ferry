"""Ferry's wordmark — the mark and block letterforms shown when the tool starts.

Compact block design, chosen 2026-08-21 after the first "wordmark + wake"
attempt read as too plain:

    ▏   ▕  ┏━╸ ┏━╸ ┏━┓ ┏━┓ ╻ ╻
    ▏━━▸▕  ┣╸  ┣╸  ┣┳┛ ┣┳┛ ┗┳┛
    ▏   ▕  ╹   ┗━╸ ╹┗╸ ╹┗╸  ╹

The mark on the left is two shores with a crossing between them — which is
what a ferry is. The letters reveal column by column on launch, left to right,
so the crossing reads as motion.

Everything has an ASCII twin, because a console reporting ``cp1252`` cannot
encode any of these glyphs and would raise rather than look wrong.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ferry import __version__
from ferry.cli.theme import ASCII_ICONS, Theme

__all__ = ["TAGLINE", "Wordmark", "build_wordmark", "render"]

TAGLINE = "carry your conversations across"

_MARK_ROWS = ("▏   ▕", "▏━━▸▕", "▏   ▕")
"""Two shores and a crossing. Three rows so it sits level with the letters."""

_LETTERS: dict[str, tuple[str, str, str]] = {
    "F": ("┏━╸", "┣╸ ", "╹  "),
    "E": ("┏━╸", "┣╸ ", "┗━╸"),
    "R": ("┏━┓", "┣┳┛", "╹┗╸"),
    "Y": ("╻ ╻", "┗┳┛", " ╹ "),
}

_ASCII_MARK_ROWS = ("|   |", "|-->|", "|   |")
_ASCII_NAME = "F E R R Y"


@dataclass(frozen=True)
class Wordmark:
    """The wordmark resolved for one theme.

    Held as rows rather than a finished string so the reveal animation can draw
    a partial number of columns without rebuilding anything.
    """

    mark_rows: tuple[str, ...]
    letter_rows: tuple[str, ...]
    version: str
    tagline: str
    ascii_only: bool

    @property
    def width(self) -> int:
        """Width of the letterform block, in columns.

        The widest row, not the first — in the ASCII form the name sits on the
        middle row and the outer two are empty, so measuring row zero gave a
        width of nought and blanked the whole wordmark.
        """
        return max(len(row) for row in self.letter_rows)

    def reveal(self, columns: int) -> tuple[str, ...]:
        """The letterform rows truncated to ``columns`` characters.

        Args:
            columns: How much of the block has been drawn. Clamped to the
                block's own width.
        """
        cut = max(0, min(columns, self.width))
        return tuple(row[:cut] for row in self.letter_rows)


def build_wordmark(theme: Theme, *, version: str = __version__) -> Wordmark:
    """Assemble the wordmark using glyphs the theme can actually render."""
    ascii_only = theme.icons is ASCII_ICONS
    if ascii_only:
        return Wordmark(
            mark_rows=_ASCII_MARK_ROWS,
            letter_rows=("", _ASCII_NAME, ""),
            version=version,
            tagline=TAGLINE,
            ascii_only=True,
        )
    rows = tuple(" ".join(_LETTERS[letter][row] for letter in "FERRY") for row in range(3))
    return Wordmark(
        mark_rows=_MARK_ROWS,
        letter_rows=rows,
        version=version,
        tagline=TAGLINE,
        ascii_only=False,
    )


def render(ui: object, theme: Theme, *, animate: bool = True) -> None:
    """Print the wordmark, revealing the letters left to right when possible.

    Args:
        ui: The :class:`~ferry.cli.ui.UI` doing the printing.
        theme: Active theme.
        animate: Whether to reveal progressively. Suppressed anyway when the
            theme carries no colour or output is not a terminal, so nothing
            writes cursor-control sequences into a log.
    """
    from rich.live import Live
    from rich.text import Text

    from ferry.cli.ui import UI

    assert isinstance(ui, UI)
    wm = build_wordmark(theme)
    console = ui.console
    coloured = theme.uses_color

    def frame(columns: int) -> Text:
        out = Text()
        letters = wm.reveal(columns)
        for index, mark_row in enumerate(wm.mark_rows):
            out.append("  ")
            out.append(mark_row, style="ferry.accent" if coloured else "")
            out.append("  ")
            out.append(letters[index], style="ferry.heading" if coloured else "")
            if index == 0 and columns >= wm.width:
                out.append(f"   {wm.version}", style="ferry.dim" if coloured else "")
            out.append("\n")
        out.append("  ")
        out.append(wm.tagline, style="ferry.dim" if coloured else "")
        out.append("\n")
        return out

    console.print()
    if not (animate and coloured and ui.interactive):
        console.print(frame(wm.width))
        return

    with Live(frame(0), console=console, refresh_per_second=60, transient=False) as live:
        for columns in range(0, wm.width + 1, 2):
            live.update(frame(columns))
            time.sleep(0.018)
        live.update(frame(wm.width))
