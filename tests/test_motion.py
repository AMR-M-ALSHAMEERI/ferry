"""Tests for animation state, and the emoji guard the icon set depends on.

The important test in this file is
:func:`test_no_allowed_glyph_carries_the_unicode_emoji_property`. Ferry shipped
four glyphs that rendered as colour emoji in a real terminal -- U+2714, U+2716,
U+25FC and U+25FB -- because "is this an emoji?" was guessed rather than
checked. ``unicodedata`` has no emoji property, so the ranges below were
extracted from the Unicode Consortium's own ``emoji-data.txt`` and are checked
offline here.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from ferry.cli.motion import (
    FRAME_SECONDS,
    MENU_MOTION,
    WAKE_PERIOD,
    Motion,
    spinner_frames,
    wake_row,
)
from ferry.cli.theme import ASCII_ICONS, TEXT_ONLY_GLYPHS, UNICODE_ICONS

BMP_EMOJI_RANGES: list[tuple[int, int]] = [
    (0x0023, 0x0023),
    (0x002A, 0x002A),
    (0x0030, 0x0039),
    (0x00A9, 0x00A9),
    (0x00AE, 0x00AE),
    (0x203C, 0x203C),
    (0x2049, 0x2049),
    (0x2122, 0x2122),
    (0x2139, 0x2139),
    (0x2194, 0x2199),
    (0x21A9, 0x21AA),
    (0x231A, 0x231B),
    (0x2328, 0x2328),
    (0x23CF, 0x23CF),
    (0x23E9, 0x23F3),
    (0x23F8, 0x23FA),
    (0x24C2, 0x24C2),
    (0x25AA, 0x25AB),
    (0x25B6, 0x25B6),
    (0x25C0, 0x25C0),
    (0x25FB, 0x25FE),
    (0x2600, 0x2604),
    (0x260E, 0x260E),
    (0x2611, 0x2611),
    (0x2614, 0x2615),
    (0x2618, 0x2618),
    (0x261D, 0x261D),
    (0x2620, 0x2620),
    (0x2622, 0x2623),
    (0x2626, 0x2626),
    (0x262A, 0x262A),
    (0x262E, 0x262F),
    (0x2638, 0x263A),
    (0x2640, 0x2640),
    (0x2642, 0x2642),
    (0x2648, 0x2653),
    (0x265F, 0x2660),
    (0x2663, 0x2663),
    (0x2665, 0x2666),
    (0x2668, 0x2668),
    (0x267B, 0x267B),
    (0x267E, 0x267F),
    (0x2692, 0x2697),
    (0x2699, 0x2699),
    (0x269B, 0x269C),
    (0x26A0, 0x26A1),
    (0x26A7, 0x26A7),
    (0x26AA, 0x26AB),
    (0x26B0, 0x26B1),
    (0x26BD, 0x26BE),
    (0x26C4, 0x26C5),
    (0x26C8, 0x26C8),
    (0x26CE, 0x26CF),
    (0x26D1, 0x26D1),
    (0x26D3, 0x26D4),
    (0x26E9, 0x26EA),
    (0x26F0, 0x26F5),
    (0x26F7, 0x26FA),
    (0x26FD, 0x26FD),
    (0x2702, 0x2702),
    (0x2705, 0x2705),
    (0x2708, 0x270D),
    (0x270F, 0x270F),
    (0x2712, 0x2712),
    (0x2714, 0x2714),
    (0x2716, 0x2716),
    (0x271D, 0x271D),
    (0x2721, 0x2721),
    (0x2728, 0x2728),
    (0x2733, 0x2734),
    (0x2744, 0x2744),
    (0x2747, 0x2747),
    (0x274C, 0x274C),
    (0x274E, 0x274E),
    (0x2753, 0x2755),
    (0x2757, 0x2757),
    (0x2763, 0x2764),
    (0x2795, 0x2797),
    (0x27A1, 0x27A1),
    (0x27B0, 0x27B0),
    (0x27BF, 0x27BF),
    (0x2934, 0x2935),
    (0x2B05, 0x2B07),
    (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50),
    (0x2B55, 0x2B55),
    (0x3030, 0x3030),
    (0x303D, 0x303D),
    (0x3297, 0x3297),
    (0x3299, 0x3299),
]
"""Every BMP codepoint carrying the Unicode ``Emoji`` property.

Extracted from https://www.unicode.org/Public/UCD/latest/ucd/emoji/emoji-data.txt
(Version 17.0) on 2026-08-22. Baked in rather than downloaded, so the suite
stays offline and CI stays hermetic. Astral-plane emoji are omitted: nothing in
Ferry's interface goes above U+FFFF.
"""


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return any(low <= cp <= high for low, high in BMP_EMOJI_RANGES)


# ---------- the emoji guard ----------


def test_the_range_table_recognises_the_glyphs_that_actually_shipped_wrong() -> None:
    """A self-test of the table: if these four are not caught, it is wrong."""
    for ch in "✔✖◼◻":
        assert _is_emoji(ch), f"U+{ord(ch):04X} should be flagged as emoji"


def test_the_range_table_does_not_flag_ordinary_geometry() -> None:
    for ch in "✓✗◆◇─≈":
        assert not _is_emoji(ch), f"U+{ord(ch):04X} wrongly flagged"


def test_no_allowed_glyph_carries_the_unicode_emoji_property() -> None:
    """The allow-list is the only thing standing between Ferry and emoji."""
    offenders = sorted(f"U+{ord(c):04X}" for c in TEXT_ONLY_GLYPHS if _is_emoji(c))
    assert offenders == [], f"emoji on the allow-list: {offenders}"


def test_every_icon_glyph_is_on_the_allow_list() -> None:
    for field in UNICODE_ICONS.__dataclass_fields__:
        for ch in getattr(UNICODE_ICONS, field):
            if not ch.isascii():
                assert ch in TEXT_ONLY_GLYPHS, f"{field}: U+{ord(ch):04X}"


def test_ascii_icon_set_is_pure_ascii() -> None:
    for field in ASCII_ICONS.__dataclass_fields__:
        assert getattr(ASCII_ICONS, field).isascii()


# ---------- wake ----------


def test_wake_row_is_exactly_the_width_asked_for() -> None:
    for width in (1, 3, 7, 40):
        assert len(wake_row(width, 0)) == width


def test_wake_row_handles_a_zero_or_negative_width() -> None:
    assert wake_row(0, 0) == ""
    assert wake_row(-3, 0) == ""


def test_wake_marks_sit_one_period_apart() -> None:
    positions = [i for i, ch in enumerate(wake_row(9, 0)) if ch == "≈"]
    assert positions == [0, 3, 6]


def test_wake_drifts_leftward_as_the_phase_advances() -> None:
    """Water flowing left means the hull is heading right, matching the reveal."""
    first = wake_row(9, 0).index("≈")
    second = wake_row(9, 1).index("≈")
    assert second == first + WAKE_PERIOD - 1


def test_wake_cycles_back_to_the_start() -> None:
    assert wake_row(9, 0) == wake_row(9, WAKE_PERIOD)


def test_wake_phase_accepts_negative_ticks() -> None:
    assert len(wake_row(7, -5)) == 7


def test_wake_can_be_drawn_in_ascii() -> None:
    assert wake_row(7, 0, mark="~").isascii()


# ---------- spinner ----------


def test_spinner_has_one_frame_per_phase() -> None:
    assert len(spinner_frames()) == WAKE_PERIOD


def test_spinner_frames_are_all_the_same_width() -> None:
    """A spinner that changes width makes the label beside it jitter."""
    assert len({len(f) for f in spinner_frames()}) == 1


def test_spinner_frames_all_differ() -> None:
    assert len(set(spinner_frames())) == WAKE_PERIOD


def test_spinner_ends_with_the_hull() -> None:
    assert all(f.endswith("▸") for f in spinner_frames())


def test_ascii_spinner_is_pure_ascii() -> None:
    assert all(f.isascii() for f in spinner_frames(ascii_only=True))


def test_spinner_glyphs_are_on_the_allow_list() -> None:
    for ch in "".join(spinner_frames()):
        if not ch.isascii():
            assert ch in TEXT_ONLY_GLYPHS


# ---------- menu motion ----------


def test_every_menu_action_has_a_motion() -> None:
    from ferry.cli.menu import MENU_ITEMS

    for value, _label in MENU_ITEMS:
        assert value in MENU_MOTION, f"{value} has no icon"


def test_no_orphan_motions() -> None:
    from ferry.cli.menu import MENU_ITEMS

    assert set(MENU_MOTION) == {value for value, _ in MENU_ITEMS}


def test_all_menu_icons_share_one_width() -> None:
    """Ragged icon widths would leave the labels unaligned down the menu."""
    assert len({m.width() for m in MENU_MOTION.values()}) == 1
    assert len({m.width(ascii_only=True) for m in MENU_MOTION.values()}) == 1


def test_menu_glyphs_are_all_on_the_allow_list() -> None:
    for name, motion in MENU_MOTION.items():
        for ch in "".join(motion.frames) + motion.rest:
            if not ch.isascii():
                assert ch in TEXT_ONLY_GLYPHS, f"{name}: U+{ord(ch):04X}"


def test_menu_ascii_frames_are_pure_ascii() -> None:
    for motion in MENU_MOTION.values():
        assert "".join(motion.ascii_frames).isascii()


def test_export_and_import_are_mirror_images() -> None:
    """The two core operations should be told apart before the labels are read."""
    assert MENU_MOTION["export"].rest.strip() == "▸"
    assert MENU_MOTION["import"].rest.strip() == "◂"


def test_quit_does_not_move() -> None:
    assert len(MENU_MOTION["quit"].frames) == 1


def test_theme_icon_holds_still_but_rotates_colour() -> None:
    motion = MENU_MOTION["theme"]
    assert len(motion.frames) == 1
    assert len(motion.styles) > 1
    assert len({motion.style(t) for t in range(len(motion.styles))}) == len(motion.styles)


# ---------- Motion behaviour ----------


def test_frames_cycle() -> None:
    motion = MENU_MOTION["compact"]
    assert motion.frame(0) == motion.frame(len(motion.frames))


def test_unselected_rows_show_the_resting_frame() -> None:
    """A resting icon should look calm, not like an animation paused mid-step."""
    motion = MENU_MOTION["export"]
    for tick in range(10):
        assert motion.frame(tick, selected=False) == motion.rest


def test_unselected_rows_are_dim_whatever_the_tick() -> None:
    for motion in MENU_MOTION.values():
        assert motion.style(3, selected=False) == "dim"


def test_ascii_mode_returns_the_ascii_frames() -> None:
    motion = MENU_MOTION["import"]
    assert motion.frame(0, ascii_only=True).isascii()
    assert motion.frame(0, selected=False, ascii_only=True) == motion.ascii_rest


def test_large_ticks_do_not_overflow() -> None:
    motion = MENU_MOTION["inspect"]
    assert motion.frame(10_000_000) in motion.frames


# ---------- Motion validation ----------


def test_ragged_frames_are_rejected() -> None:
    """Frames of differing width would shift the label sideways each tick."""
    with pytest.raises(ValueError, match="ragged"):
        Motion(frames=("ab", "c"), ascii_frames=("ab", "cd"), rest="ab", ascii_rest="ab")


def test_rest_frame_must_match_the_frame_width() -> None:
    with pytest.raises(ValueError, match="ragged"):
        Motion(frames=("ab",), ascii_frames=("ab",), rest="abc", ascii_rest="ab")


def test_non_ascii_ascii_frames_are_rejected() -> None:
    with pytest.raises(ValueError, match="ascii"):
        Motion(frames=("ab",), ascii_frames=("≈b",), rest="ab", ascii_rest="ab")


def test_an_empty_motion_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one frame"):
        Motion(frames=(), ascii_frames=("ab",), rest="ab", ascii_rest="ab")


def test_a_motion_with_no_styles_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one frame"):
        Motion(frames=("ab",), ascii_frames=("ab",), rest="ab", ascii_rest="ab", styles=())


def test_frame_interval_is_slow_enough_not_to_strobe() -> None:
    """A fast repaint loop flickers over SSH; 8fps reads as motion regardless."""
    assert FRAME_SECONDS >= 0.1


# ---------- no stray non-ASCII anywhere else ----------

GLYPH_MODULES = {"theme.py", "motion.py", "brand.py"}
"""The only modules allowed to contain non-ASCII string literals.

Everywhere else must reach glyphs through the icon set, so they degrade on a
console that cannot encode them.
"""


def _documentation_nodes(tree: ast.AST) -> set[int]:
    """Ids of string constants that are docstrings or attribute docs.

    Prose in a docstring is never printed, so it is free to contain anything.
    """
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }


def test_no_module_hard_codes_a_glyph_outside_the_icon_set() -> None:
    """Regression: the key-hint footer, the scan separator and several messages
    hard-coded ``·`` and ``—``. The em dash has no ``cp437`` mapping, so a
    console that had already fallen back to ASCII icons would still have raised
    ``UnicodeEncodeError`` on ordinary prose."""
    import ferry

    root = pathlib.Path(ferry.__file__).parent
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name in GLYPH_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = _documentation_nodes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docs or node.value.isascii():
                continue
            stray = "".join(sorted({c for c in node.value if not c.isascii()}))
            offenders.append(f"{path.name}:{node.lineno} {stray!r}")
    assert offenders == [], f"hard-coded non-ASCII outside the icon set: {offenders}"


def test_both_icon_sets_define_every_field() -> None:
    """A field added to one set and forgotten in the other crashes at runtime."""
    assert UNICODE_ICONS.__dataclass_fields__.keys() == ASCII_ICONS.__dataclass_fields__.keys()


def test_the_probe_covers_every_unicode_icon() -> None:
    """A glyph missing from the probe is a glyph that can crash undetected."""
    from ferry.cli.theme import _UNICODE_PROBE

    for field in UNICODE_ICONS.__dataclass_fields__:
        for ch in getattr(UNICODE_ICONS, field):
            if not ch.isascii():
                assert ch in _UNICODE_PROBE, f"{field}: U+{ord(ch):04X} not probed"


def test_cp437_console_falls_back_because_of_the_em_dash() -> None:
    """cp437 can encode `·` but not `—`, which is why prose had to be guarded."""
    assert "·".encode("cp437")
    with pytest.raises(UnicodeEncodeError):
        "—".encode("cp437")
