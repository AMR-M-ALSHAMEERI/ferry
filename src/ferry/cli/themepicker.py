"""The theme picker — arrow between themes and see each one render live.

Chosen 2026-08-21 (PROGRESS.md ledger, M2 second design pass): the preview
re-renders in the highlighted theme on every cursor move, so the user judges a
theme by looking at it rather than by reading its name.

The sample deliberately shows the pieces a theme actually affects: the
wordmark, a detected and an undetected tool, a progress bar, and a menu row.
"""

from __future__ import annotations

import dataclasses

from ferry.cli.brand import build_wordmark
from ferry.cli.prompts import Fragments, SelectorItem, _style_for, run_select
from ferry.cli.theme import ASCII_ICONS, THEMES, Theme

__all__ = ["THEME_ORDER", "pick_theme", "theme_preview"]

THEME_ORDER = ["harbor", "compass", "classic", "mono"]
"""Display order: identity themes first, then the neutral ones."""

_DESCRIPTIONS = {
    "harbor": "teal and amber",
    "compass": "sky and indigo",
    "classic": "your terminal's own colours",
    "mono": "no colour, plain ASCII",
}


def theme_preview(theme: Theme, *, width: int = 30) -> Fragments:
    """Render a sample of what this theme looks like.

    Args:
        theme: The theme to demonstrate.
        width: Width of the sample progress bar.

    Returns:
        prompt_toolkit fragments, ready to append to a frame.
    """
    icons = theme.icons
    dim = _style_for(theme, "dim")
    text = _style_for(theme, "text")
    primary = _style_for(theme, "primary")
    accent = _style_for(theme, "accent")
    success = _style_for(theme, "success")

    wm = build_wordmark(theme)
    filled = int(width * 0.62)

    out: Fragments = [
        (dim, "  " + icons.progress_track * 4 + " preview " + icons.progress_track * 4 + "\n\n"),
        (accent, f"  {wm.mark}  "),
        (primary, wm.name),
        (dim, f"   {wm.version}\n"),
        (primary, "  " + wm.wake_char * filled),
        (dim, wm.wake_char * (width - filled) + "\n"),
        (dim, f"  {wm.tagline}\n\n"),
        (success, f"  {icons.success} "),
        (text, "Claude Code"),
        (accent, "   12 conversations\n"),
        (dim, f"  {icons.error} OpenAI Codex   not found\n\n"),
        (primary, "  " + icons.progress_fill * filled),
        (dim, icons.progress_track * (width - filled)),
        (accent, "  62%\n"),
        (primary, f"  {icons.cursor} "),
        (primary, "Export conversations\n"),
        ("", "    "),
        (text, "Import a bundle\n"),
    ]
    return out


def pick_theme(active: Theme) -> str | None:
    """Show the picker and return the chosen theme name.

    Args:
        active: The theme currently in use. Used for the picker's own chrome and
            highlighted on open. Passed in rather than re-resolved, so the
            picker inherits any capability downgrade already applied.

    Returns:
        The chosen theme name, or ``None`` if the user cancelled.
    """
    items = [
        SelectorItem(name, THEMES[name].name, _DESCRIPTIONS.get(name, "")) for name in THEME_ORDER
    ]
    initial = THEME_ORDER.index(active.name) if active.name in THEME_ORDER else 0

    def preview(item: SelectorItem) -> Fragments:
        candidate = THEMES[item.value]
        if active.icons is ASCII_ICONS and candidate.icons is not ASCII_ICONS:
            # This console cannot encode the Unicode glyphs, so previewing them
            # would crash. Show the candidate's colours with ASCII markers.
            candidate = dataclasses.replace(candidate, icons=ASCII_ICONS)
        return theme_preview(candidate)

    return run_select(
        "Choose a theme - the preview updates as you move",
        items,
        theme=active,
        preview=preview,
        allow_filter=False,
        initial=initial,
    )
