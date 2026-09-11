"""Draw Ferry's wordmark as SVG, for the README.

The mark the terminal shows is built from box-drawing characters. An SVG made
of those characters as text would depend on whatever font the reader has, and
box-drawing glyphs are exactly where fonts disagree, so this draws every
character as the strokes it stands for: a line from the middle of its cell to
each edge it touches, heavy or light, with the hull's two rounded corners and
the wake's waves drawn as curves.

Two files come out, one for dark backgrounds and one for light. The README
shows the right one for each reader's theme.

Run from the repository root::

    python scripts/make_logo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from ferry.cli.brand import TAGLINE, build_wordmark
from ferry.cli.theme import HARBOR

CELL_W = 12.0
CELL_H = 24.0
HEAVY = 3.4
LIGHT = 2.0
PAD = 8.0

OUT = Path(__file__).resolve().parents[1] / "docs" / "assets"

PALETTES = {
    "dark": {"mark": HARBOR.accent, "letters": HARBOR.primary, "tagline": HARBOR.dim},
    "light": {"mark": "#d97706", "letters": "#0f766e", "tagline": "#64748b"},
}
"""Harbor's own colours on dark; deeper versions of the same hues on light,
where the pale teal would all but disappear on white."""

# Which edges each character reaches from its cell's middle: up, right, down, left.
HEAVY_GLYPHS: dict[str, str] = {
    "━": "rl",
    "┏": "rd",
    "┓": "ld",
    "┗": "ur",
    "┛": "ul",
    "┣": "urd",
    "┳": "rdl",
    "╸": "l",
    "╹": "u",
    "╻": "d",
}
LIGHT_GLYPHS: dict[str, str] = {"─": "rl", "╷": "d"}


def _stroke(x: float, y: float, edges: str, width: float, colour: str) -> list[str]:
    cx, cy = x + CELL_W / 2, y + CELL_H / 2
    ends = {"u": (cx, y), "d": (cx, y + CELL_H), "l": (x, cy), "r": (x + CELL_W, cy)}
    cap = "square" if width == HEAVY else "butt"
    return [
        f'<line x1="{cx:.2f}" y1="{cy:.2f}" x2="{ends[e][0]:.2f}" y2="{ends[e][1]:.2f}" '
        f'stroke="{colour}" stroke-width="{width}" stroke-linecap="{cap}"/>'
        for e in edges
    ]


def _corner(x: float, y: float, towards: str, colour: str) -> str:
    """A light rounded corner from the cell's top edge round to one side."""
    cx, cy = x + CELL_W / 2, y + CELL_H / 2
    r = CELL_W / 2
    side = x + CELL_W if towards == "r" else x
    bend = cx + r if towards == "r" else cx - r
    return (
        f'<path d="M{cx:.2f},{y:.2f} L{cx:.2f},{cy - r:.2f} Q{cx:.2f},{cy:.2f} {bend:.2f},{cy:.2f} '
        f'L{side:.2f},{cy:.2f}" fill="none" stroke="{colour}" stroke-width="{LIGHT}"/>'
    )


def _wave(x: float, y: float, colour: str) -> str:
    """The wake's mark: two small waves stacked, as the terminal's approximately-equal sign."""
    left, right = x + 1.5, x + CELL_W - 1.5
    mid = (left + right) / 2
    paths = []
    for dy in (-3.2, 3.2):
        base = y + CELL_H / 2 + dy
        crest = (left + mid) / 2
        paths.append(
            f"M{left:.2f},{base + 1.2:.2f} Q{crest:.2f},{base - 2.4:.2f} {mid:.2f},{base:.2f} "
            f"T{right:.2f},{base - 1.2:.2f}"
        )
    return (
        f'<path d="{" ".join(paths)}" fill="none" stroke="{colour}" stroke-width="{LIGHT}" '
        'stroke-linecap="round"/>'
    )


def _cell(char: str, x: float, y: float, colour: str) -> list[str]:
    if char in HEAVY_GLYPHS:
        return _stroke(x, y, HEAVY_GLYPHS[char], HEAVY, colour)
    if char in LIGHT_GLYPHS:
        return _stroke(x, y, LIGHT_GLYPHS[char], LIGHT, colour)
    if char == "╰":
        return [_corner(x, y, "r", colour)]
    if char == "╯":
        return [_corner(x, y, "l", colour)]
    if char == "≈":
        return [_wave(x, y, colour)]
    if char.strip():
        raise ValueError(f"no drawing for {char!r}")
    return []


def draw(palette: dict[str, str]) -> str:
    mark = build_wordmark(HARBOR)
    gap = 2
    letters_at = len(mark.mark_rows(0)[0]) + gap
    columns = letters_at + mark.width
    shapes: list[str] = []
    for row, (mark_row, letter_row) in enumerate(
        zip(mark.mark_rows(0), mark.letter_rows, strict=True)
    ):
        y = PAD + row * CELL_H
        for col, char in enumerate(mark_row):
            shapes += _cell(char, PAD + col * CELL_W, y, palette["mark"])
        for col, char in enumerate(letter_row):
            shapes += _cell(char, PAD + (letters_at + col) * CELL_W, y, palette["letters"])

    width = PAD * 2 + columns * CELL_W
    tagline_y = PAD + 3 * CELL_H + 22
    height = tagline_y + PAD + 4
    shapes.append(
        f'<text x="{PAD + CELL_W / 2:.2f}" y="{tagline_y:.2f}" fill="{palette["tagline"]}" '
        'font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" '
        f'font-size="15">{TAGLINE}</text>'
    )
    body = "\n  ".join(shapes)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'width="{width:.0f}" height="{height:.0f}" role="img" aria-label="Ferry: {TAGLINE}">\n'
        f"  <title>Ferry</title>\n  {body}\n</svg>\n"
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, palette in PALETTES.items():
        target = OUT / f"ferry-logo-{name}.svg"
        target.write_text(draw(palette), encoding="utf-8", newline="\n")
        print(f"wrote {target.relative_to(OUT.parents[1])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
