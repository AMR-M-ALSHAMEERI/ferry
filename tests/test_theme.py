"""Tests for theme selection, capability detection, and the fallback chain."""

from __future__ import annotations

import dataclasses
import io

import pytest

from ferry.cli.theme import (
    ASCII_ICONS,
    CLASSIC,
    COMPASS,
    DEFAULT_THEME,
    HARBOR,
    MONO,
    TEXT_ONLY_GLYPHS,
    THEMES,
    UNICODE_ICONS,
    Capability,
    IconSet,
    Theme,
    detect_capability,
    resolve_theme,
    supports_unicode,
)


class _Tty(io.StringIO):
    """A stream that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


class _Pipe(io.StringIO):
    """A stream that claims not to be a terminal."""

    def isatty(self) -> bool:
        return False


def test_all_four_themes_registered() -> None:
    assert sorted(THEMES) == ["classic", "compass", "harbor", "mono"]


def test_default_is_harbor() -> None:
    assert DEFAULT_THEME == "harbor"
    assert THEMES[DEFAULT_THEME] is HARBOR


@pytest.mark.parametrize("theme", [HARBOR, COMPASS, CLASSIC])
def test_coloured_themes_report_using_colour(theme: Theme) -> None:
    assert theme.uses_color is True


def test_mono_reports_no_colour() -> None:
    assert MONO.uses_color is False


def test_every_icon_slot_is_populated_in_every_theme() -> None:
    """A blank glyph would render as a gap with no indication of status."""
    slots = [f.name for f in dataclasses.fields(IconSet)]
    for name, theme in THEMES.items():
        for slot in slots:
            value = getattr(theme.icons, slot)
            assert value, f"{name}.{slot} is empty"


def test_mono_icons_are_pure_ascii() -> None:
    """Regression: a Unicode ellipsis in mono output was mangled to byte 0x8D
    when piped to a file on Windows. mono must stay inside ASCII."""
    for field in dataclasses.fields(IconSet):
        glyph = getattr(ASCII_ICONS, field.name)
        assert glyph.isascii(), f"{field.name}={glyph!r} is not ASCII"


def test_every_unicode_icon_is_on_the_allow_list() -> None:
    """Emoji were ruled out (ledger #62), but "is this an emoji?" cannot be
    computed — the emoji-capable characters are scattered through the BMP.

    Regression: the original check was `ord(ch) < 0x1F000`, which passed four
    glyphs that carry the Unicode Emoji property and rendered as colour emoji
    in the user's terminal — U+2714, U+2716, U+25FC and U+25FB.
    """
    for field in dataclasses.fields(IconSet):
        glyph = getattr(UNICODE_ICONS, field.name)
        for ch in glyph:
            assert ch in TEXT_ONLY_GLYPHS, f"{field.name} uses {ch!r} (U+{ord(ch):04X})"


@pytest.mark.parametrize("codepoint", [0x2714, 0x2716, 0x25FC, 0x25FB, 0x25B6, 0x25AA])
def test_known_emoji_capable_glyphs_are_not_allowed(codepoint: int) -> None:
    """The specific characters that shipped by mistake stay banned."""
    assert chr(codepoint) not in TEXT_ONLY_GLYPHS


def test_the_probe_covers_exactly_the_allow_list() -> None:
    """One source of truth: anything printable must be probe-tested."""
    from ferry.cli.theme import _UNICODE_PROBE

    assert set(_UNICODE_PROBE) == set(TEXT_ONLY_GLYPHS)


def test_tty_with_colour_detected() -> None:
    assert detect_capability(stream=_Tty(), env={}) is Capability.COLOR


def test_pipe_detected_as_plain() -> None:
    assert detect_capability(stream=_Pipe(), env={}) is Capability.PLAIN


@pytest.mark.parametrize("var", ["NO_COLOR", "FERRY_NO_COLOR"])
def test_no_color_env_vars_honoured(var: str) -> None:
    """The NO_COLOR convention is set-means-on, whatever the value."""
    assert detect_capability(stream=_Tty(), env={var: ""}) is Capability.NO_COLOR


def test_dumb_terminal_drops_colour() -> None:
    assert detect_capability(stream=_Tty(), env={"TERM": "dumb"}) is Capability.NO_COLOR


def test_requested_theme_used_when_terminal_supports_colour() -> None:
    assert resolve_theme("compass", capability=Capability.COLOR, env={}) is COMPASS


def test_env_var_used_when_no_flag_given() -> None:
    theme = resolve_theme(None, capability=Capability.COLOR, env={"FERRY_THEME": "classic"})
    assert theme is CLASSIC


def test_flag_beats_env_var() -> None:
    theme = resolve_theme("harbor", capability=Capability.COLOR, env={"FERRY_THEME": "compass"})
    assert theme is HARBOR


def test_unknown_theme_name_falls_back_to_default_without_raising() -> None:
    """A typo in --theme must not stop someone migrating their conversations."""
    assert resolve_theme("nonsense", capability=Capability.COLOR, env={}) is HARBOR


def test_theme_name_is_case_and_space_insensitive() -> None:
    assert resolve_theme("  HaRbOr ", capability=Capability.COLOR, env={}) is HARBOR


@pytest.mark.parametrize("requested", ["harbor", "compass", "classic", "mono"])
@pytest.mark.parametrize("cap", [Capability.NO_COLOR, Capability.PLAIN])
def test_every_theme_degrades_to_mono_without_colour(requested: str, cap: Capability) -> None:
    """Whatever was asked for, a terminal that cannot show colour gets mono —
    otherwise escape codes end up in log files."""
    assert resolve_theme(requested, capability=cap, env={}) is MONO


# ---------- unicode encodability guard ----------


class _Encoded(io.StringIO):
    """A terminal stream that reports a specific output encoding."""

    def __init__(self, encoding: str) -> None:
        super().__init__()
        self._encoding = encoding

    @property  # type: ignore[override]
    def encoding(self) -> str:
        return self._encoding

    def isatty(self) -> bool:
        return True


def test_utf8_stream_supports_the_icon_set() -> None:
    assert supports_unicode(_Encoded("utf-8")) is True


@pytest.mark.parametrize("encoding", ["cp1252", "ascii", "latin-1"])
def test_legacy_encodings_cannot_carry_the_icon_set(encoding: str) -> None:
    """Regression: on a Windows console reporting cp1252, printing a Unicode
    glyph raises UnicodeEncodeError and takes the whole program down."""
    assert supports_unicode(_Encoded(encoding)) is False


def test_unknown_encoding_name_is_treated_as_unsupported() -> None:
    assert supports_unicode(_Encoded("not-a-real-codec")) is False


def test_stream_without_an_encoding_attribute_is_unsupported() -> None:
    assert supports_unicode(io.StringIO()) is False


def test_colour_survives_the_icon_downgrade() -> None:
    """A cp1252 console still gets the palette — only the glyphs drop to ASCII."""
    theme = resolve_theme("harbor", capability=Capability.COLOR, env={}, stream=_Encoded("cp1252"))
    assert theme.icons is ASCII_ICONS
    assert theme.uses_color is True
    assert theme.primary == HARBOR.primary


def test_unicode_icons_kept_when_the_stream_can_encode_them() -> None:
    theme = resolve_theme("harbor", capability=Capability.COLOR, env={}, stream=_Encoded("utf-8"))
    assert theme.icons is UNICODE_ICONS


@pytest.mark.parametrize("name", ["harbor", "compass", "classic"])
def test_every_coloured_theme_downgrades_its_icons(name: str) -> None:
    theme = resolve_theme(name, capability=Capability.COLOR, env={}, stream=_Encoded("cp1252"))
    assert theme.icons is ASCII_ICONS
    assert theme.name == name
