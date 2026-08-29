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
from prompt_toolkit.completion import PathCompleter
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.lexers import SimpleLexer
from prompt_toolkit.shortcuts import CompleteStyle, PromptSession
from prompt_toolkit.styles import Style

from ferry.cli.motion import MENU_FRAME_SECONDS, Motion
from ferry.cli.theme import ASCII_ICONS, Theme

__all__ = [
    "Fragment",
    "Fragments",
    "SelectorItem",
    "SelectorModel",
    "build_bindings",
    "path_bindings",
    "path_header",
    "path_hints",
    "path_style",
    "secret_bindings",
    "secret_hints",
    "run_confirm",
    "run_path",
    "run_secret",
    "run_select",
]

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
        hint: Optional dim text after the label, on the same line. For a few
            words -- "teal and amber". Anything longer pushes the row past the
            width of a terminal and wraps into the next one.
        note: Optional sentence explaining this choice, shown **in the panel
            below the list** for whichever row the cursor is on -- never under
            every row at once.

            Two options that differ only in what they cost cannot say so in
            three words, and a label that tries reads as vague: *"leaving
            anything already there alone"* drew the question "what is *there*?"
            from the person it was written for. But a sentence under every row
            doubles the height of the list and buries the labels in prose, so
            only the row being considered explains itself. One question, one
            answer, in one place that does not move.
        motion: Optional animated icon. When set it *replaces* the cursor
            glyph for this list — an animated marker already says where the
            cursor is, and showing both reads as clutter.
    """

    value: str
    label: str
    hint: str = ""
    note: str = ""
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
    current: str | None = None,
    back: bool = False,
) -> Fragments:
    """Build the frame shown on each redraw.

    Args:
        tick: Animation tick. Only the row under the cursor animates — six
            icons moving at once is noise, and animating just the selected one
            doubles as a second cursor indicator for the same redraw cost.
        current: Value of the option already in force, marked "in use".
        back: Whether escape returns to the previous step rather than leaving.
            The footer says which, because a key that goes back while the
            screen says "cancel" is the same fault as one that works and is
            never mentioned.
    """
    icons = theme.icons
    ascii_only = icons is ASCII_ICONS
    sep = icons.separator
    dim = _style_for(theme, "dim")
    text = _style_for(theme, "text")
    primary = _style_for(theme, "primary")
    accent = _style_for(theme, "accent")

    # One marker cell, wide enough for every marker this list can show, so the
    # title and all the labels land in a single column. The widths genuinely
    # differ: `[i]` is three characters where `>` is one, and an animated icon
    # is three where a chevron is one. Padding each to a shared width is what
    # keeps the text edge straight.
    cell = max(
        len(icons.cursor),
        len(icons.info),
        *(
            item.motion.width(ascii_only=ascii_only)
            for item in model.items
            if item.motion is not None
        ),
    )

    def marker(glyph: str, style: str) -> Fragments:
        """One marker centred in the shared cell, with its trailing space.

        Centred rather than left-aligned so a one-character marker lines up
        with the body of the three-character icons rather than sitting a column
        to their left.
        """
        return [(style, "  " + glyph.center(cell) + " ")]

    out: Fragments = []
    if title:
        out += marker(icons.info, dim)
        out += [(text, title), ("", "\n\n")]

    visible = model.visible
    if not visible:
        out += [(dim, f"  no match for {model.filter!r}\n")]
    for index, item in enumerate(visible):
        selected = index == min(model.cursor, len(visible) - 1)
        label_style = primary if selected else (dim if model.filtering else text)
        if item.motion is not None:
            glyph = item.motion.frame(tick, selected=selected, ascii_only=ascii_only)
            style = _style_for(theme, item.motion.style(tick, selected=selected))
            out += marker(glyph, style)
            # The selected row's label takes its own icon's colour rather than
            # one highlight colour for everything, so choosing an action tells
            # you what kind of action it is: Compact warns, Quit reds, Change
            # theme cycles with its swatch. Unselected rows stay neutral unless
            # the motion asks otherwise.
            if selected:
                label_style = style
            elif not model.filtering and item.motion.rest_style:
                label_style = _style_for(theme, item.motion.rest_style)
            out += [(label_style, item.label)]
        elif selected:
            out += marker(icons.cursor, primary)
            out += [(primary, item.label)]
        else:
            out += marker("", "")
            out += [(label_style, item.label)]
        if item.hint:
            out += [(dim, f"   {item.hint}")]
        if item.value == current:
            # Which option is already in force is a different fact from which
            # one the cursor is on, and a picker that only shows the cursor
            # leaves you guessing what you would be changing away from.
            out += [(accent, f"   {icons.selected} in use")]
        out += [("", "\n")]

    highlighted = model.current
    if preview is not None and highlighted is not None:
        out += [("", "\n")]
        out += preview(highlighted)

    out += [("", "\n")]
    # The footer names what the keys do *here*, because what escape does
    # depends on where you are: with a filter in force it clears the filter,
    # and only then does it leave the screen.
    if model.filtering:
        out += [(accent, "  / "), (text, model.filter), (primary, "_")]
        if model.visible:
            out += [(dim, f"    enter apply  {sep}  esc cancel filter\n")]
        else:
            out += [(dim, f"    backspace to edit  {sep}  esc cancel filter\n")]
    elif model.filter:
        keys = f"up/down move  {sep}  enter select  {sep}  esc clear filter"
        out += [(dim, f"  {keys}\n")]
    else:
        leave = "esc back" if back else "esc cancel"
        keys = f"up/down move  {sep}  enter select  {sep}  {leave}"
        if allow_filter:
            keys = f"up/down move  {sep}  enter select  {sep}  / filter  {sep}  {leave}"
        out += [(dim, f"  {keys}\n")]
    return out


def build_bindings(model: SelectorModel, *, allow_filter: bool = True) -> KeyBindings:
    """The picker's keys, built against a model so they can be tested.

    Lifted out of :func:`run_select` because the behaviour that matters most
    here -- what enter and escape do when a filter matches nothing -- was
    unreachable by any test while it lived inside a function that also builds
    an application and takes over the terminal. The bug it now guards against
    quit Ferry outright.
    """
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
        """Take the highlighted item -- and never take "nothing" for an answer.

        A filter matching nothing leaves no item highlighted. Exiting with
        ``None`` there would be read by the caller as a cancel, so typing a
        word that happens to match no option and pressing enter **quit Ferry**.
        Nobody asked to leave; they mistyped.

        So an empty list keeps the filter open instead, where backspace still
        works and the text is still visible to correct.
        """
        if model.filtering:
            if not model.visible:
                return
            model.stop_filtering(clear=False)
            return
        chosen = model.current
        if chosen is None:
            # Not filtering, nothing highlighted: a filter is hiding
            # everything. Clear it rather than cancelling out of the screen.
            model.set_filter("")
            return
        event.app.exit(result=chosen.value)

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    def _cancel(event) -> None:  # type: ignore[no-untyped-def]
        """Back out one level at a time, which is what escape is expected to do.

        A filter can be *applied* without filter mode being open -- press
        enter on a filter and the text stays in force. Escape used to cancel
        the whole screen from there, which on the main menu means quitting
        Ferry with no warning. It now clears the filter first; escape again
        leaves.
        """
        if model.filtering:
            model.stop_filtering()
            return
        if model.filter:
            model.set_filter("")
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

    return kb


def run_select(
    title: str,
    items: Sequence[SelectorItem],
    *,
    theme: Theme,
    preview: Callable[[SelectorItem], Fragments] | None = None,
    allow_filter: bool = True,
    initial: int = 0,
    current: str | None = None,
    back: bool = False,
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
        current: Value of the option already in force, marked "in use". Any
            picker that changes a persistent setting should pass this — the
            cursor says what you are looking at, not what you are looking at
            *instead of*.
        back: Whether escape returns to the previous step rather than leaving
            the screen. Changes only the footer -- both cases still return
            ``None`` -- but a screen that goes back while saying "cancel" is
            why people stop pressing escape at all.

    Returns:
        The chosen item's value, or ``None`` if the user cancelled or went
        back. The caller knows which of those it asked for.
    """
    model = SelectorModel(items, initial=initial)
    animated = theme.uses_color and any(item.motion is not None for item in items)
    started = time.monotonic()

    def tick() -> int:
        """Frames elapsed since the picker opened."""
        if not animated:
            return 0
        return int((time.monotonic() - started) / MENU_FRAME_SECONDS)

    kb = build_bindings(model, allow_filter=allow_filter)

    control = FormattedTextControl(
        lambda: _render(model, theme, title, preview, allow_filter, tick(), current, back),
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


def path_bindings() -> KeyBindings:
    """The keys for the path prompt, built apart so they can be tested.

    **Escape cancels.** It did not, which is the whole reason this prompt is
    Ferry's own rather than questionary's: questionary builds its own bindings
    and passes them to ``PromptSession`` as a constructor argument, so there is
    nowhere to add one afterwards. Someone who reached the "type a path" prompt
    by accident -- which is exactly where an empty bundle list sends them --
    had no way back to the menu except typing something and hoping.

    Enter on an empty line cancels too, for the same reason: an empty answer is
    not a path, and the caller reads ``None`` as "went back".
    """
    keys = KeyBindings()

    @keys.add("escape", eager=True)
    @keys.add("c-c")
    def _cancel(event) -> None:  # type: ignore[no-untyped-def]
        event.app.exit(result=None)

    @keys.add("enter")
    def _accept(event) -> None:  # type: ignore[no-untyped-def]
        buffer = event.current_buffer
        if buffer.complete_state is not None:
            # First enter takes the completion, not the answer. Otherwise a
            # half-typed path is accepted the moment someone confirms a
            # suggestion.
            buffer.complete_state = None
            return
        text = buffer.document.text.strip()
        event.app.exit(result=text or None)

    return keys


def path_hints(theme: Theme, *, has_default: bool) -> Fragments:
    """The key line under the path prompt.

    Every picker in Ferry names its keys along the bottom, and this prompt did
    not -- so the one screen where escape had just been fixed was also the one
    screen that never said escape was available. A key that works and is not
    mentioned is a key nobody presses.
    """
    dim = _style_for(theme, "dim")
    sep = theme.icons.separator
    keys = [f"tab completes  {sep}  enter accept"]
    if has_default:
        keys.append(f"{sep}  enter alone takes the suggestion")
    keys.append(f"{sep}  esc cancel")
    return [(dim, "  " + " ".join(keys))]


def path_header(theme: Theme, question: str) -> Fragments:
    """The prompt line itself, built apart so its styles can be checked.

    They could not be, and were wrong: this asked :func:`_style_for` for
    ``"ferry.dim"`` and ``"ferry.text"`` where it keys on ``"dim"`` and
    ``"text"``. An unknown token returns an empty style rather than raising, so
    the prompt rendered in the terminal's default and looked like a theme
    choice rather than a mistake.
    """
    return [
        (_style_for(theme, "dim"), f"  {theme.icons.info} "),
        (_style_for(theme, "text"), f"{question}  "),
    ]


def path_style(theme: Theme) -> Style:
    """Colours for the path prompt, taken from the theme like everything else.

    The typed path is the **accent** -- amber under harbor -- for the same
    reason the filter text is: it is the part the person is producing, and it
    should be the thing their eye lands on. It was rendering in the terminal's
    default, which under harbor made this the one prompt in Ferry that ignored
    the theme entirely.

    The token names matter and are easy to get wrong: :func:`_style_for` keys
    on ``"accent"``, not ``"ferry.accent"``. Passing the prefixed name returns
    an empty style, which is not an error and looks exactly like a theme with
    no colours.
    """
    accent = _style_for(theme, "accent")
    dim = _style_for(theme, "dim")
    primary = _style_for(theme, "primary")
    return Style.from_dict(
        {
            "answer": accent,
            # The toolbar is a plain line under the prompt, not a reversed bar
            # across the terminal -- prompt_toolkit's default would put a solid
            # block where every other Ferry screen has a quiet hint.
            "bottom-toolbar": f"noreverse {dim}".strip(),
            "bottom-toolbar.text": f"noreverse {dim}".strip(),
            "completion-menu.completion": dim,
            "completion-menu.completion.current": f"reverse {accent}".strip(),
            "scrollbar.background": dim,
            "scrollbar.button": primary,
        }
    )


def run_path(question: str, *, theme: Theme, default: str = "") -> str | None:
    """Ask for a filesystem path, with tab completion.

    The one prompt in Ferry that takes typing, because a folder that does not
    exist yet cannot be offered as a choice. Everything else about it matches
    the pickers: the same icon, the same styles, a key line along the bottom,
    and escape goes back.

    Args:
        default: Pre-filled, and **passed to** :meth:`PromptSession.prompt`
            rather than written into the buffer beforehand. ``prompt()`` resets
            the buffer itself as it starts, so a default set in advance is
            silently wiped -- which is how the export screen lost the suggested
            bundle name it had offered since M2.

    Returns:
        The path, or ``None`` if the user backed out.
    """
    session: PromptSession[str | None] = PromptSession(
        path_header(theme, question),
        completer=PathCompleter(expanduser=True),
        complete_style=CompleteStyle.MULTI_COLUMN,
        key_bindings=path_bindings(),
        lexer=SimpleLexer("class:answer"),
        bottom_toolbar=lambda: path_hints(theme, has_default=bool(default)),
        style=path_style(theme),
    )
    return session.prompt(default=default)


def secret_bindings() -> KeyBindings:
    """The keys for the passphrase prompt.

    The same shape as :func:`path_bindings` and for the same reason: escape has
    to back out. A passphrase prompt someone cannot leave is worse than most,
    because they reached it while trying to protect something and now cannot
    stop.

    There is no completion here, so enter always means enter.
    """
    keys = KeyBindings()

    @keys.add("escape", eager=True)
    @keys.add("c-c")
    def _cancel(event) -> None:  # type: ignore[no-untyped-def]
        event.app.exit(result=None)

    @keys.add("enter")
    def _accept(event) -> None:  # type: ignore[no-untyped-def]
        # Not stripped. Leading and trailing spaces are part of a passphrase,
        # and silently trimming them produces a bundle that will not open with
        # what the person believes they typed.
        text = event.current_buffer.document.text
        event.app.exit(result=text or None)

    return keys


def secret_hints(theme: Theme) -> Fragments:
    """The key line under the passphrase prompt."""
    sep = theme.icons.separator
    return [(_style_for(theme, "dim"), f"  nothing is shown as you type  {sep}  esc cancel")]


def run_secret(question: str, *, theme: Theme) -> str | None:
    """Ask for a passphrase without echoing it.

    Returns:
        The passphrase, or ``None`` if the user backed out or entered nothing.
        An empty passphrase is deliberately the same as backing out: it is not
        encryption, it is a rename.
    """
    session: PromptSession[str | None] = PromptSession(
        path_header(theme, question),
        is_password=True,
        key_bindings=secret_bindings(),
        bottom_toolbar=lambda: secret_hints(theme),
        style=path_style(theme),
    )
    return session.prompt()


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
