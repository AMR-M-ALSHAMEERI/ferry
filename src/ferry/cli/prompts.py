"""Arrow-driven selection, with an optional live preview and a slash filter.

The user should never have to type an identifier or a `y`/`n` (PROGRESS.md
ledger #70, #71). Every choice in Ferry is made with the arrow keys, and this
module is where that behaviour lives.

The split here is deliberate. :class:`SelectorModel` holds all the state —
which item is under the cursor, what the filter is, what is visible — as plain
Python that unit tests can drive. :func:`run_select` is a thin prompt_toolkit
renderer over it. Full-screen terminal applications cannot be meaningfully
unit tested, so nothing that matters is allowed to live inside one.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl

from ferry.cli.motion import MENU_FRAME_SECONDS, Motion
from ferry.cli.theme import ASCII_ICONS, Theme

__all__ = ["Fragment", "Fragments", "SelectorItem", "SelectorModel", "run_confirm", "run_select"]

Fragment = tuple[str, str]
"""One prompt_toolkit ``(style, text)`` pair."""

Fragments = StyleAndTextTuples
"""A list of fragments, using prompt_toolkit's own alias.

Ours would be ``list[tuple[str, str]]``, which mypy rejects at the
prompt_toolkit boundary: their list also admits 3-tuples carrying a mouse
handler, and list is invariant.
"""


@dataclass(frozen=True)
class SelectorItem:
    """One choosable row.

    Args:
        value: What :func:`run_select` returns when this row is chosen.
        label: Text shown to the user, and what the ``/`` filter matches.
        hint: Optional dim text after the label.
        motion: Optional animated icon. When set it *replaces* the cursor
            glyph for this list — an animated marker already says where the
            cursor is, and showing both reads as clutter.
    """

    value: str
    label: str
    hint: str = ""
    motion: Motion | None = None


class SelectorModel:
    """Cursor position and filtering for a list of choices.

    Pure state, no rendering. Filtering is a case-insensitive substring match
    against each item's label and value.
    """

    def __init__(self, items: Sequence[SelectorItem], *, initial: int = 0) -> None:
        if not items:
            raise ValueError("a selector needs at least one item")
        self._items = list(items)
        self._filter = ""
        self.cursor = max(0, min(initial, len(self._items) - 1))
        self.filtering = False

    @property
    def items(self) -> list[SelectorItem]:
        """Every item, ignoring the filter."""
        return list(self._items)

    @property
    def filter(self) -> str:
        """The active filter text."""
        return self._filter

    @property
    def visible(self) -> list[SelectorItem]:
        """Items matching the current filter, in original order."""
        if not self._filter:
            return list(self._items)
        needle = self._filter.casefold()
        return [
            item
            for item in self._items
            if needle in item.label.casefold() or needle in item.value.casefold()
        ]

    @property
    def current(self) -> SelectorItem | None:
        """The item under the cursor, or ``None`` if the filter matches nothing."""
        visible = self.visible
        if not visible:
            return None
        return visible[min(self.cursor, len(visible) - 1)]

    def move(self, delta: int) -> None:
        """Move the cursor, wrapping around both ends.

        Wrapping matters for a short menu: pressing up on the first entry should
        reach the last one rather than doing nothing.
        """
        count = len(self.visible)
        if count == 0:
            self.cursor = 0
            return
        self.cursor = (self.cursor + delta) % count

    def set_filter(self, text: str) -> None:
        """Replace the filter and move the cursor back to the first match."""
        self._filter = text
        self.cursor = 0

    def start_filtering(self) -> None:
        """Enter filter mode, triggered by ``/``."""
        self.filtering = True

    def stop_filtering(self, *, clear: bool = True) -> None:
        """Leave filter mode, optionally discarding what was typed."""
        self.filtering = False
        if clear:
            self.set_filter("")


_ANSI_NAMES: dict[str, str] = {
    "black": "fg:ansiblack",
    "red": "fg:ansired",
    "green": "fg:ansigreen",
    "yellow": "fg:ansiyellow",
    "blue": "fg:ansiblue",
    "magenta": "fg:ansimagenta",
    "cyan": "fg:ansicyan",
    "white": "fg:ansiwhite",
    "bright_black": "fg:ansibrightblack",
    "bright_red": "fg:ansibrightred",
    "bright_green": "fg:ansibrightgreen",
    "bright_yellow": "fg:ansibrightyellow",
    "bright_blue": "fg:ansibrightblue",
    "bright_magenta": "fg:ansibrightmagenta",
    "bright_cyan": "fg:ansibrightcyan",
    "bright_white": "fg:ansiwhite",
    "default": "",
}
"""rich colour names mapped to prompt_toolkit's.

The two libraries spell the bright colours differently — rich says
``bright_black``, prompt_toolkit says ``ansibrightblack``. Naively prefixing
``ansi`` produced ``ansibright_black``, which prompt_toolkit rejects with
``ValueError: Wrong color format``. That crashed the classic theme the moment
it was selected, since classic is the only theme using ANSI names.
"""


def _style_for(theme: Theme, token: str) -> str:
    """Map a theme colour token to a prompt_toolkit style string.

    Returns an empty style under ``mono``, which prompt_toolkit renders as the
    terminal's default — the same "no styling" behaviour the rest of the
    interface uses.
    """
    if not theme.uses_color:
        return ""
    value = {
        "primary": theme.primary,
        "accent": theme.accent,
        "success": theme.success,
        "error": theme.error,
        "warning": theme.warning,
        "dim": theme.dim,
        "text": theme.text,
    }.get(token, "")
    if not value:
        return ""
    if value.startswith("#"):
        return f"fg:{value}"
    return _ANSI_NAMES.get(value, "")


def _render(
    model: SelectorModel,
    theme: Theme,
    title: str,
    preview: Callable[[SelectorItem], Fragments] | None,
    allow_filter: bool,
    tick: int = 0,
) -> Fragments:
    """Build the frame shown on each redraw.

    Args:
        tick: Animation tick. Only the row under the cursor animates — six
            icons moving at once is noise, and animating just the selected one
            doubles as a second cursor indicator for the same redraw cost.
    """
    icons = theme.icons
    ascii_only = icons is ASCII_ICONS
    sep = icons.separator
    dim = _style_for(theme, "dim")
    text = _style_for(theme, "text")
    primary = _style_for(theme, "primary")
    accent = _style_for(theme, "accent")

    out: Fragments = []
    if title:
        out += [(dim, f"  {icons.info} "), (text, title), ("", "\n\n")]

    visible = model.visible
    if not visible:
        out += [(dim, f"  no match for {model.filter!r}\n")]
    for index, item in enumerate(visible):
        selected = index == min(model.cursor, len(visible) - 1)
        if item.motion is not None:
            glyph = item.motion.frame(tick, selected=selected, ascii_only=ascii_only)
            token = item.motion.style(tick, selected=selected)
            out += [(_style_for(theme, token), f"  {glyph} ")]
            out += [(primary if selected else (dim if model.filtering else text), item.label)]
        elif selected:
            out += [(primary, f"  {icons.cursor} "), (primary, item.label)]
        else:
            out += [("", "    "), (dim if model.filtering else text, item.label)]
        if item.hint:
            out += [(dim, f"   {item.hint}")]
        out += [("", "\n")]

    current = model.current
    if preview is not None and current is not None:
        out += [("", "\n")]
        out += preview(current)

    out += [("", "\n")]
    if model.filtering:
        out += [(accent, "  / "), (text, model.filter), (primary, "_")]
        out += [(dim, f"    enter apply  {sep}  esc cancel filter\n")]
    else:
        keys = f"up/down move  {sep}  enter select  {sep}  esc cancel"
        if allow_filter:
            keys = f"up/down move  {sep}  enter select  {sep}  / filter  {sep}  esc cancel"
        out += [(dim, f"  {keys}\n")]
    return out


def run_select(
    title: str,
    items: Sequence[SelectorItem],
    *,
    theme: Theme,
    preview: Callable[[SelectorItem], Fragments] | None = None,
    allow_filter: bool = True,
    initial: int = 0,
) -> str | None:
    """Show an arrow-driven picker and return the chosen value.

    Args:
        title: Question shown above the list.
        items: Choices, in display order.
        theme: Active theme, used for both colour and glyphs.
        preview: Optional callback rendering a live preview of the highlighted
            item. Called on every cursor move, so it must be cheap.
        allow_filter: Whether ``/`` opens the filter.
        initial: Index highlighted on open.

    Returns:
        The chosen item's value, or ``None`` if the user cancelled.
    """
    model = SelectorModel(items, initial=initial)
    animated = theme.uses_color and any(item.motion is not None for item in items)
    started = time.monotonic()

    def tick() -> int:
        """Frames elapsed since the picker opened."""
        if not animated:
            return 0
        return int((time.monotonic() - started) / MENU_FRAME_SECONDS)

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("c-p")
    def _up(event: object) -> None:
        model.move(-1)

    @kb.add("down")
    @kb.add("c-n")
    def _down(event: object) -> None:
        model.move(1)

    @kb.add("enter")
    def _accept(event) -> None:  # type: ignore[no-untyped-def]
        if model.filtering:
            model.stop_filtering(clear=False)
            return
        chosen = model.current
        event.app.exit(result=chosen.value if chosen else None)

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    def _cancel(event) -> None:  # type: ignore[no-untyped-def]
        if model.filtering:
            model.stop_filtering()
            return
        event.app.exit(result=None)

    if allow_filter:
        not_filtering = Condition(lambda: not model.filtering)

        @kb.add("/", filter=not_filtering)
        def _slash(event: object) -> None:
            model.start_filtering()

        @kb.add("backspace")
        def _backspace(event: object) -> None:
            if model.filtering:
                model.set_filter(model.filter[:-1])

        @kb.add("<any>")
        def _typed(event) -> None:  # type: ignore[no-untyped-def]
            if not model.filtering:
                return
            char = event.data
            if char and char.isprintable():
                model.set_filter(model.filter + char)

    control = FormattedTextControl(
        lambda: _render(model, theme, title, preview, allow_filter, tick()),
        focusable=True,
        show_cursor=False,
    )
    app: Application[str | None] = Application(
        layout=Layout(HSplit([Window(control, always_hide_cursor=True)])),
        key_bindings=kb,
        full_screen=False,
        erase_when_done=True,
        # prompt_toolkit redraws itself on this interval, which is all the
        # animation needs — no background task, no thread poking invalidate().
        # Left unset when nothing moves, so a static picker costs no repaints.
        refresh_interval=MENU_FRAME_SECONDS if animated else 0.0,
    )
    return app.run()


def run_confirm(
    question: str,
    *,
    theme: Theme,
    default: bool,
    yes_label: str = "Yes",
    no_label: str = "No",
) -> bool | None:
    """Ask a yes/no question with the arrow keys — never by typing a letter.

    Args:
        default: Which option starts under the cursor. Read-only actions should
            default to yes; anything writing to a user's real data should
            default to no.

    Returns:
        ``True``, ``False``, or ``None`` if cancelled.
    """
    items = [SelectorItem("yes", yes_label), SelectorItem("no", no_label)]
    chosen = run_select(
        question,
        items,
        theme=theme,
        allow_filter=False,
        initial=0 if default else 1,
    )
    if chosen is None:
        return None
    return chosen == "yes"
