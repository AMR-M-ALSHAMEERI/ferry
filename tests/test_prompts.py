"""Tests for selector state, the wordmark, config storage, and theme previews.

The prompt_toolkit renderer itself is not tested here — a full-screen terminal
application cannot be driven meaningfully from pytest. That is exactly why the
state lives in :class:`SelectorModel`, which can be.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from ferry.cli.brand import TAGLINE, build_wordmark
from ferry.cli.prompts import SelectorItem, SelectorModel, _style_for
from ferry.cli.theme import (
    ASCII_ICONS,
    HARBOR,
    MONO,
    TEXT_ONLY_GLYPHS,
    THEMES,
    UNICODE_ICONS,
)
from ferry.cli.themepicker import THEME_ORDER, theme_preview
from ferry.config import load_config, read_setting, save_config, write_setting

ITEMS = [
    SelectorItem("export", "Export conversations to a bundle"),
    SelectorItem("import", "Import a bundle into a tool"),
    SelectorItem("theme", "Change theme"),
    SelectorItem("quit", "Quit"),
]


# ---------- selector state ----------


def test_empty_selector_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one item"):
        SelectorModel([])


def test_cursor_starts_on_the_requested_item() -> None:
    assert SelectorModel(ITEMS, initial=2).current is not None
    assert SelectorModel(ITEMS, initial=2).current.value == "theme"


def test_out_of_range_initial_is_clamped() -> None:
    assert SelectorModel(ITEMS, initial=99).current.value == "quit"


def test_moving_down_advances() -> None:
    m = SelectorModel(ITEMS)
    m.move(1)
    assert m.current.value == "import"


def test_cursor_wraps_at_the_top() -> None:
    """Pressing up on the first entry should reach the last, not stick."""
    m = SelectorModel(ITEMS)
    m.move(-1)
    assert m.current.value == "quit"


def test_cursor_wraps_at_the_bottom() -> None:
    m = SelectorModel(ITEMS, initial=len(ITEMS) - 1)
    m.move(1)
    assert m.current.value == "export"


def test_filter_matches_labels_case_insensitively() -> None:
    m = SelectorModel(ITEMS)
    m.set_filter("BUNDLE")
    assert [i.value for i in m.visible] == ["export", "import"]


def test_filter_matches_the_value_too() -> None:
    """`/theme` should find "Change theme" even though the label differs."""
    m = SelectorModel(ITEMS)
    m.set_filter("theme")
    assert [i.value for i in m.visible] == ["theme"]


def test_filter_resets_the_cursor_to_the_first_match() -> None:
    m = SelectorModel(ITEMS, initial=3)
    m.set_filter("bundle")
    assert m.current.value == "export"


def test_no_match_leaves_nothing_current() -> None:
    m = SelectorModel(ITEMS)
    m.set_filter("nonsense")
    assert m.visible == []
    assert m.current is None


def test_moving_with_no_matches_does_not_crash() -> None:
    m = SelectorModel(ITEMS)
    m.set_filter("nonsense")
    m.move(1)
    assert m.current is None


def test_clearing_the_filter_restores_everything() -> None:
    m = SelectorModel(ITEMS)
    m.set_filter("quit")
    m.stop_filtering()
    assert len(m.visible) == len(ITEMS)
    assert m.filtering is False


def test_stop_filtering_can_keep_the_filter() -> None:
    m = SelectorModel(ITEMS)
    m.start_filtering()
    m.set_filter("theme")
    m.stop_filtering(clear=False)
    assert m.filtering is False
    assert m.filter == "theme"


# ---------- wordmark ----------


def test_wordmark_uses_block_letters_when_the_theme_can() -> None:
    wm = build_wordmark(HARBOR)
    assert wm.ascii_only is False
    assert len(wm.letter_rows) == 3
    assert len(wm.mark_rows()) == 3
    assert wm.tagline == TAGLINE


def test_block_rows_are_all_the_same_width() -> None:
    """Ragged rows would make the letterforms lean during the reveal."""
    wm = build_wordmark(HARBOR)
    assert len({len(row) for row in wm.letter_rows}) == 1


def test_wordmark_falls_back_to_ascii_with_ascii_icons() -> None:
    """A cp1252 console gets a wordmark it can actually print."""
    wm = build_wordmark(dataclasses.replace(HARBOR, icons=ASCII_ICONS))
    assert wm.ascii_only is True
    joined = "".join(wm.mark_rows()) + "".join(wm.letter_rows) + wm.tagline
    assert joined.isascii()


def test_ascii_wordmark_width_comes_from_the_widest_row() -> None:
    """Regression: width was read from row 0, which is empty in the ASCII form,
    so reveal() truncated everything to nothing and the name vanished."""
    wm = build_wordmark(dataclasses.replace(HARBOR, icons=ASCII_ICONS))
    assert wm.width == len("F E R R Y")
    assert "F E R R Y" in "".join(wm.reveal(wm.width))


def test_reveal_truncates_every_row_equally() -> None:
    wm = build_wordmark(HARBOR)
    rows = wm.reveal(6)
    assert {len(row) for row in rows} == {6}


@pytest.mark.parametrize("columns", [-5, 0, 999])
def test_reveal_clamps_out_of_range_positions(columns: int) -> None:
    wm = build_wordmark(HARBOR)
    rows = wm.reveal(columns)
    assert all(len(row) <= wm.width for row in rows)


def test_every_wordmark_glyph_is_on_the_allow_list() -> None:
    wm = build_wordmark(HARBOR)
    for ch in "".join(wm.mark_rows()) + "".join(wm.letter_rows):
        if not ch.isascii():
            assert ch in TEXT_ONLY_GLYPHS, f"{ch!r} (U+{ord(ch):04X}) is not allowed"


# ---------- theme preview ----------


def test_every_theme_has_a_preview() -> None:
    for name in THEME_ORDER:
        assert theme_preview(THEMES[name])


def test_ascii_themes_produce_ascii_previews() -> None:
    """A preview containing a Unicode glyph would crash a cp1252 console."""
    for theme in (MONO, dataclasses.replace(HARBOR, icons=ASCII_ICONS)):
        text = "".join(fragment[1] for fragment in theme_preview(theme))
        assert text.isascii(), [c for c in text if not c.isascii()]


def test_unicode_themes_use_the_unicode_glyphs() -> None:
    text = "".join(fragment[1] for fragment in theme_preview(HARBOR))
    assert UNICODE_ICONS.success in text
    assert UNICODE_ICONS.cursor in text


def test_previews_only_use_allowed_glyphs() -> None:
    """A preview glyph outside the allow-list would slip past the probe."""
    for name in THEME_ORDER:
        text = "".join(fragment[1] for fragment in theme_preview(THEMES[name]))
        for ch in text:
            if not ch.isascii():
                assert ch in TEXT_ONLY_GLYPHS, f"{name}: {ch!r} (U+{ord(ch):04X})"


def test_absent_marker_used_for_missing_tools_not_the_error_glyph() -> None:
    """ "Not installed" is information, not a failure."""
    text = "".join(fragment[1] for fragment in theme_preview(HARBOR))
    assert f"{UNICODE_ICONS.absent} OpenAI Codex" in text
    assert f"{UNICODE_ICONS.error} OpenAI Codex" not in text


def test_mono_preview_carries_no_styling() -> None:
    styles = {fragment[0] for fragment in theme_preview(MONO)}
    assert styles == {""}


def test_coloured_preview_carries_styling() -> None:
    styles = {fragment[0] for fragment in theme_preview(HARBOR)}
    assert any(s.startswith("fg:") for s in styles)


def test_style_lookup_is_empty_under_mono() -> None:
    assert _style_for(MONO, "primary") == ""


def test_style_lookup_maps_hex_colours() -> None:
    assert _style_for(HARBOR, "primary") == f"fg:{HARBOR.primary}"


# ---------- config ----------


def test_missing_config_reads_as_empty(tmp_path) -> None:
    assert load_config(tmp_path / "nope.json") == {}


def test_corrupt_config_reads_as_empty_rather_than_raising(tmp_path) -> None:
    """A broken settings file must never block someone migrating history."""
    path = tmp_path / "config.json"
    path.write_text("{not json at all", encoding="utf-8")
    assert load_config(path) == {}


def test_non_object_config_reads_as_empty(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_config(path) == {}


def test_round_trip(tmp_path) -> None:
    path = tmp_path / "config.json"
    assert save_config({"theme": "compass"}, path) is True
    assert load_config(path) == {"theme": "compass"}


def test_write_setting_preserves_other_keys(tmp_path) -> None:
    path = tmp_path / "config.json"
    save_config({"theme": "harbor", "other": 1}, path)
    write_setting("theme", "mono", path=path)
    assert load_config(path) == {"theme": "mono", "other": 1}


def test_read_setting_falls_back_to_default(tmp_path) -> None:
    assert read_setting("theme", "harbor", path=tmp_path / "nope.json") == "harbor"


def test_saved_file_is_valid_json(tmp_path) -> None:
    path = tmp_path / "config.json"
    write_setting("theme", "classic", path=path)
    assert json.loads(path.read_text(encoding="utf-8"))["theme"] == "classic"


def test_save_into_an_unwritable_location_reports_failure(tmp_path) -> None:
    """Failing to save a preference is reported, never raised."""
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    assert save_config({"theme": "harbor"}, blocker / "config.json") is False


def test_saved_theme_is_used_when_nothing_overrides_it(tmp_path, monkeypatch) -> None:
    """The picker persists a choice; the next run must honour it."""
    from ferry.cli.theme import COMPASS, Capability, resolve_theme

    path = tmp_path / "config.json"
    write_setting("theme", "compass", path=path)
    monkeypatch.setattr("ferry.config.CONFIG_PATH", path)
    assert resolve_theme(None, capability=Capability.COLOR, env={}) is COMPASS


def test_flag_beats_the_saved_theme(tmp_path, monkeypatch) -> None:
    from ferry.cli.theme import HARBOR as H
    from ferry.cli.theme import Capability, resolve_theme

    path = tmp_path / "config.json"
    write_setting("theme", "compass", path=path)
    monkeypatch.setattr("ferry.config.CONFIG_PATH", path)
    assert resolve_theme("harbor", capability=Capability.COLOR, env={}) is H


def test_corrupt_saved_theme_does_not_break_resolution(tmp_path, monkeypatch) -> None:
    from ferry.cli.theme import HARBOR as H
    from ferry.cli.theme import Capability, resolve_theme

    path = tmp_path / "config.json"
    path.write_text('{"theme": 42}', encoding="utf-8")
    monkeypatch.setattr("ferry.config.CONFIG_PATH", path)
    assert resolve_theme(None, capability=Capability.COLOR, env={}) is H


# ---------- style strings must be valid prompt_toolkit ----------


def test_every_theme_produces_parseable_prompt_toolkit_styles() -> None:
    """Regression: classic's `bright_black` became `ansibright_black`, which
    prompt_toolkit rejects with ValueError. Selecting the theme crashed."""
    from prompt_toolkit.styles.style import parse_color

    from ferry.cli.theme import THEMES as ALL

    for theme in ALL.values():
        for token in ("primary", "accent", "success", "error", "warning", "dim", "text"):
            style = _style_for(theme, token)
            if not style:
                continue
            colour = style.split(":", 1)[1]
            parse_color(colour)  # raises ValueError if the name is wrong


def test_classic_dim_maps_to_the_prompt_toolkit_spelling() -> None:
    from ferry.cli.theme import CLASSIC

    assert _style_for(CLASSIC, "dim") == "fg:ansibrightblack"


def test_unknown_colour_name_yields_no_style_rather_than_a_bad_one() -> None:
    """Better to lose a colour than to emit a style that crashes the picker."""
    theme = dataclasses.replace(HARBOR, primary="chartreuse")
    assert _style_for(theme, "primary") == ""


# ---------- animated menu icons ----------


def _text_of(fragments) -> str:
    return "".join(f[1] for f in fragments)


def _menu_model():
    from ferry.cli.motion import MENU_MOTION

    items = [
        SelectorItem("export", "Export", motion=MENU_MOTION["export"]),
        SelectorItem("quit", "Quit", motion=MENU_MOTION["quit"]),
    ]
    return SelectorModel(items), items


def test_the_selected_row_animates_and_the_others_do_not() -> None:
    from ferry.cli.prompts import _render

    model, items = _menu_model()
    export = items[0].motion
    assert export is not None
    seen = {
        _text_of(_render(model, HARBOR, "", None, False, tick)).split("\n")[0] for tick in range(4)
    }
    assert len(seen) > 1, "the selected row should change between ticks"


def test_an_unselected_row_holds_its_resting_frame() -> None:
    from ferry.cli.prompts import _render

    model, items = _menu_model()
    quit_motion = items[1].motion
    assert quit_motion is not None
    for tick in range(6):
        rows = _text_of(_render(model, HARBOR, "", None, False, tick)).split("\n")
        assert quit_motion.rest in rows[1]


def test_the_icon_replaces_the_cursor_glyph() -> None:
    """Showing an animated marker *and* a chevron reads as clutter."""
    from ferry.cli.prompts import _render

    model, _ = _menu_model()
    text = _text_of(_render(model, HARBOR, "", None, False, 0))
    assert UNICODE_ICONS.cursor not in text


def test_lists_without_motion_still_get_the_cursor_glyph() -> None:
    from ferry.cli.prompts import _render

    text = _text_of(_render(SelectorModel(ITEMS), HARBOR, "", None, False, 0))
    assert UNICODE_ICONS.cursor in text


def test_menu_rows_stay_aligned_across_every_tick() -> None:
    """A width change anywhere in the icon would make the labels jitter."""
    from ferry.cli.prompts import _render

    model, _ = _menu_model()
    for tick in range(12):
        for row in _text_of(_render(model, HARBOR, "", None, False, tick)).split("\n"):
            if "Export" in row:
                assert row.index("Export") == 5, repr(row)
            if "Quit" in row:
                assert row.index("Quit") == 5, repr(row)


def test_ascii_consoles_get_ascii_icons() -> None:
    from ferry.cli.prompts import _render

    model, _ = _menu_model()
    theme = dataclasses.replace(HARBOR, icons=ASCII_ICONS)
    for tick in range(6):
        assert _text_of(_render(model, theme, "", None, False, tick)).isascii()


def test_menu_icon_glyphs_are_all_on_the_allow_list() -> None:
    from ferry.cli.prompts import _render

    model, _ = _menu_model()
    for tick in range(12):
        for ch in _text_of(_render(model, HARBOR, "", None, False, tick)):
            if not ch.isascii():
                assert ch in TEXT_ONLY_GLYPHS, f"U+{ord(ch):04X}"
