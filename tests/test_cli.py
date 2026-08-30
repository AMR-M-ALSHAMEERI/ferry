"""Tests for the CLI shell: adapter registry, scan rendering, and the non-TTY guard."""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ferry.adapters.base import (
    REGISTRY,
    Adapter,
    DetectResult,
    ExportEvent,
    ImportEvent,
    ImportOptions,
    NotImplementedAdapter,
    get_adapter,
    list_adapters,
)
from ferry.cli import app
from ferry.cli.brand import TAGLINE, build_wordmark
from ferry.cli.menu import _describe, scan
from ferry.cli.theme import ASCII_ICONS, HARBOR, MONO, UNICODE_ICONS, Capability
from ferry.cli.ui import UI, NonInteractiveError

runner = CliRunner()


# ---------- registry ----------


def test_all_four_tools_registered_in_implementation_order() -> None:
    assert [a.name for a in list_adapters()] == [
        "claude-code",
        "codex",
        "copilot",
        "antigravity",
    ]


def test_get_adapter_returns_the_named_one() -> None:
    assert get_adapter("codex").display_name == "OpenAI Codex"


def test_get_adapter_raises_on_unknown_name() -> None:
    with pytest.raises(KeyError):
        get_adapter("cursor")


def test_the_implemented_slots_hold_real_adapters_now() -> None:
    """M3 and M4 each replaced a stub. Registration lives in ``ferry.adapters``."""
    from ferry.adapters.antigravity import AntigravityAdapter
    from ferry.adapters.claude_code import ClaudeCodeAdapter
    from ferry.adapters.codex import CodexAdapter
    from ferry.adapters.copilot import CopilotAdapter

    assert isinstance(get_adapter("claude-code"), ClaudeCodeAdapter)
    assert isinstance(get_adapter("codex"), CodexAdapter)
    assert isinstance(get_adapter("copilot"), CopilotAdapter)
    assert isinstance(get_adapter("antigravity"), AntigravityAdapter)


def test_no_adapter_is_a_stub_any_more() -> None:
    """Every tool in the registry has a real implementation as of M6.

    This assertion used to name the adapters still waiting, and was updated at
    each milestone. There are none left, so what it now guards is the opposite:
    a stub reappearing in the registry would mean a tool silently reporting
    itself as not installed on a machine that has it.
    """
    pending = [a for a in list_adapters() if isinstance(a, NotImplementedAdapter)]
    assert pending == []


def test_export_and_import_reach_a_real_adapter() -> None:
    """They were stubs saying "arrives at M3" long after M3 and M4 had shipped.

    The assertion that replaced that message is now itself obsolete: the
    actions no longer print anything, they run. What must stay true is that
    none of them is treated as unbuilt -- and that every menu item is either
    wired or honestly named as unbuilt, with nothing falling between.
    """
    from ferry.cli.menu import _MILESTONE_FOR_ACTION, _WIRED, MENU_ITEMS

    assert _WIRED == {"export", "import", "inspect", "compact"}
    assert not (_WIRED & set(_MILESTONE_FOR_ACTION))

    accounted = _WIRED | set(_MILESTONE_FOR_ACTION) | {"theme", "quit"}
    assert {action for action, _ in MENU_ITEMS} <= accounted


def test_an_action_that_really_is_unbuilt_still_names_its_milestone() -> None:
    """Compact was the last unbuilt action, so `_MILESTONE_FOR_ACTION` is now
    empty -- and the mechanism is still worth a test, because the next stub to
    be added should say when it arrives rather than shrugging."""
    from ferry.cli import menu

    ui = _plain_ui()
    buf = io.StringIO()
    ui.console.file = buf
    menu._stub(ui, "somethingelse")
    assert "a later milestone" in buf.getvalue()

    buf.truncate(0)
    buf.seek(0)
    menu._MILESTONE_FOR_ACTION["somethingelse"] = "M12"
    try:
        menu._stub(ui, "somethingelse")
    finally:
        del menu._MILESTONE_FOR_ACTION["somethingelse"]

    assert "M12" in buf.getvalue()


def test_stub_export_and_import_raise_not_implemented() -> None:
    stub = NotImplementedAdapter("x", "Tool X", "M9")
    with pytest.raises(NotImplementedError, match="M9"):
        list(stub.export(Path(".")))
    with pytest.raises(NotImplementedError, match="M9"):
        list(stub.import_(Path("."), ImportOptions()))


def test_import_options_default_to_backing_up() -> None:
    """Losing a user's history is the worst failure this project has."""
    assert ImportOptions().backup is True
    assert ImportOptions().dry_run is False
    assert ImportOptions().on_conflict == "skip"
    assert ImportOptions().allow_cross_tool is False


# ---------- scan rendering ----------


class _Fake(Adapter):
    """A stand-in adapter whose detect() result the test controls."""

    def __init__(self, name: str, result: DetectResult | Exception) -> None:
        self.name = name
        self.display_name = name.title()
        self._result = result

    def detect(self) -> DetectResult:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    def export(self, dest_bundle_dir: Path) -> Iterator[ExportEvent]:
        yield ExportEvent(kind="done")

    def import_(self, bundle_dir: Path, options: ImportOptions) -> Iterator[ImportEvent]:
        yield ImportEvent(kind="done")


def _plain_ui() -> UI:
    return UI(MONO, capability=Capability.PLAIN)


def test_describe_uses_singular_for_one_conversation() -> None:
    r = DetectResult(installed=True, conversation_count_estimate=1)
    assert _describe(r, UNICODE_ICONS) == "1 conversation"


def test_describe_uses_plural_otherwise() -> None:
    r = DetectResult(installed=True, conversation_count_estimate=4)
    assert _describe(r, UNICODE_ICONS) == "4 conversations"


def test_describe_includes_version_when_known() -> None:
    r = DetectResult(installed=True, version="2.1.0", conversation_count_estimate=2)
    assert _describe(r, UNICODE_ICONS) == "2.1.0 · 2 conversations"
    assert _describe(r, ASCII_ICONS) == "2.1.0 - 2 conversations"


def test_describe_surfaces_the_reason_when_not_installed() -> None:
    r = DetectResult(installed=False, notes=["adapter not yet implemented"])
    assert _describe(r, UNICODE_ICONS) == "adapter not yet implemented"


def test_scan_survives_an_adapter_that_raises() -> None:
    """detect() is contractually forbidden from raising, but one that does must
    not stop the user reaching the other tools."""
    ui = _plain_ui()
    adapters = [
        _Fake("good", DetectResult(installed=True, conversation_count_estimate=3)),
        _Fake("bad", RuntimeError("boom")),
    ]
    results = scan(ui, adapters)

    assert len(results) == 2
    assert results[0][1].installed is True
    assert results[1][1].installed is False
    assert "detection failed: RuntimeError" in results[1][1].notes[0]


# ---------- non-interactive guard ----------


def test_prompts_refuse_without_a_terminal_instead_of_hanging() -> None:
    """A prompt with no TTY would block a script or CI job forever."""
    ui = _plain_ui()
    for call in (
        lambda: ui.select("pick", [("a", "A")], hint="use --tool"),
        lambda: ui.multiselect("pick", [("a", "A")], hint="use --tool"),
        lambda: ui.confirm("sure?", default=False, hint="use --yes"),
        lambda: ui.path("where?", hint="use --output"),
    ):
        with pytest.raises(NonInteractiveError) as excinfo:
            call()
        assert excinfo.value.hint


def test_bare_ferry_without_a_tty_exits_two_and_explains() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "interactive terminal" in result.output
    assert "--help" in result.output


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == "ferry 0.1.0"


def test_unknown_theme_flag_is_rejected_with_the_valid_names() -> None:
    result = runner.invoke(app, ["--theme", "neon"])
    assert result.exit_code != 0
    for name in ("harbor", "compass", "classic", "mono"):
        assert name in result.output


def test_tools_command_lists_every_adapter_and_exits_one_when_none_found() -> None:
    result = runner.invoke(app, ["tools"])
    assert result.exit_code == 1
    for adapter in REGISTRY.values():
        assert adapter.display_name in result.output


def test_tools_output_is_pure_ascii_when_not_a_tty() -> None:
    """Regression: a Unicode ellipsis reached piped output and was mangled to
    byte 0x8D on Windows. Redirected output must stay ASCII."""
    result = runner.invoke(app, ["tools"])
    assert result.output.isascii(), [c for c in result.output if not c.isascii()]


def test_a_long_detection_note_is_wrapped_not_ellipsised() -> None:
    """Regression: the real Claude Code adapter put a path in its notes.

    Rich truncates an over-long cell with a Unicode ellipsis, which walked
    straight past the ASCII guard the whole theme system exists to enforce --
    on a cp437 console that byte is unprintable. Folding keeps every character
    the caller supplied and adds none of its own.
    """
    from ferry.cli.ui import _DetectionRow

    ui = _plain_ui()
    buf = io.StringIO()
    ui.console.file = buf
    ui.console.width = 40

    ui.detection_table([_DetectionRow("Claude Code", True, "/" + "verylongsegment/" * 12)])

    assert buf.getvalue().isascii(), [c for c in buf.getvalue() if not c.isascii()]
    assert "verylongsegment" in buf.getvalue()


def test_no_escape_codes_reach_piped_output() -> None:
    result = runner.invoke(app, ["tools"])
    assert "\x1b[" not in result.output


# ---------- ui plumbing ----------


def test_ui_reports_not_interactive_when_plain() -> None:
    assert _plain_ui().interactive is False


def test_ui_is_interactive_on_a_colour_terminal() -> None:
    assert UI(HARBOR, capability=Capability.COLOR).interactive is True


def test_mono_style_wrapping_emits_no_markup() -> None:
    ui = _plain_ui()
    assert ui._style("ferry.success", "ok") == "ok"


def test_coloured_style_wrapping_emits_markup() -> None:
    ui = UI(HARBOR, capability=Capability.COLOR)
    assert ui._style("ferry.success", "ok") == "[ferry.success]ok[/ferry.success]"


def test_debug_is_silent_unless_verbose() -> None:
    out = io.StringIO()
    ui = UI(MONO, capability=Capability.PLAIN, verbose=False)
    ui.console.file = out
    ui.debug("hidden")
    assert out.getvalue() == ""


def test_debug_prints_under_verbose() -> None:
    out = io.StringIO()
    ui = UI(MONO, capability=Capability.PLAIN, verbose=True)
    ui.console.file = out
    ui.debug("shown")
    assert "shown" in out.getvalue()


def test_progress_counts_advances_without_a_terminal() -> None:
    ui = _plain_ui()
    with ui.progress("exporting", total=3) as bar:
        bar.advance()
        bar.advance(2)
        assert bar.done == 3


# ---------- menu loop ----------


class _ScriptedUI(UI):
    """A UI whose menu answers are supplied in advance.

    Lets the menu loop be exercised without a terminal — the loop is real
    behaviour (it repeats until quit, and stubs must not end the session), so it
    is worth testing rather than leaving uncovered.
    """

    def __init__(self, answers: list[str | None]) -> None:
        super().__init__(MONO, capability=Capability.PLAIN)
        self._answers = list(answers)
        self.asked = 0
        self.last_motions = None

    def select(  # type: ignore[override]
        self, question, choices, *, hint="", allow_filter=True, motions=None
    ):
        self.asked += 1
        self.last_motions = motions
        return self._answers.pop(0)


def _menu_output(ui: UI) -> str:
    buf = io.StringIO()
    ui.console.file = buf
    return buf.getvalue()


def test_the_menu_passes_its_animated_icons_through() -> None:
    """Without this the menu silently renders with no icons at all."""
    from ferry.cli.menu import MENU_ITEMS, run_menu
    from ferry.cli.motion import MENU_MOTION

    ui = _ScriptedUI(["quit"])
    run_menu(ui)
    assert ui.last_motions is MENU_MOTION
    assert set(ui.last_motions) == {value for value, _ in MENU_ITEMS}


def test_menu_quits_cleanly_and_returns_zero() -> None:
    from ferry.cli.menu import run_menu

    ui = _ScriptedUI(["quit"])
    assert run_menu(ui) == 0
    assert ui.asked == 1


def test_menu_treats_cancel_as_quit() -> None:
    """A prompt returns None when the user presses escape or Ctrl+C."""
    from ferry.cli.menu import run_menu

    ui = _ScriptedUI([None])
    assert run_menu(ui) == 0


def test_menu_keeps_looping_after_a_stub_action() -> None:
    """Picking an unimplemented action must return to the menu, not exit."""
    from ferry.cli.menu import run_menu

    ui = _ScriptedUI(["export", "import", "inspect", "compact", "quit"])
    assert run_menu(ui) == 0
    assert ui.asked == 5


def test_menu_reports_when_nothing_is_detected() -> None:
    from ferry.cli.menu import run_menu

    ui = _ScriptedUI(["quit"])
    buf = io.StringIO()
    ui.console.file = buf
    run_menu(ui)
    assert "No supported assistants detected" in buf.getvalue()


def test_menu_exits_two_when_prompting_is_impossible() -> None:
    """A plain UI cannot prompt; the loop must give up rather than hang."""
    from ferry.cli.menu import run_menu

    assert run_menu(_plain_ui()) == 2


# ---------- the sailing spinner and progress column ----------


class _FakeTask:
    """Enough of a ``rich`` Task for the wake column to render."""

    def __init__(self, finished: bool) -> None:
        self.finished = finished


def test_wake_column_frames_are_a_constant_width() -> None:
    """A column that changes width would shove the whole progress bar sideways."""
    from ferry.cli.ui import _WakeColumn

    column = _WakeColumn(ascii_only=False)
    widths = {len(f) for f in column.frames} | {len(column.moored)}
    assert len(widths) == 1


def test_wake_column_shows_the_moored_hull_once_finished() -> None:
    """The bar completing and the ferry arriving are the same event."""
    from ferry.cli.ui import _WakeColumn

    column = _WakeColumn(ascii_only=False)
    rendered = column.render(_FakeTask(finished=True))
    assert rendered.plain == column.moored
    assert rendered.style == "ferry.success"


def test_wake_column_shows_a_moving_hull_while_running() -> None:
    from ferry.cli.ui import _WakeColumn

    column = _WakeColumn(ascii_only=False)
    assert column.render(_FakeTask(finished=False)).plain in column.frames


def test_ascii_wake_column_stays_ascii() -> None:
    from ferry.cli.ui import _WakeColumn

    column = _WakeColumn(ascii_only=True)
    assert "".join(column.frames).isascii()
    assert column.moored.isascii()
    assert column.render(_FakeTask(finished=True)).plain.isascii()


def test_wake_column_repaints_no_faster_than_the_frame_rate() -> None:
    from ferry.cli.motion import FRAME_SECONDS
    from ferry.cli.ui import _WakeColumn

    assert _WakeColumn.max_refresh == FRAME_SECONDS


def test_sailing_spinner_renders_the_label_beside_a_frame() -> None:
    from ferry.cli.ui import _SailingSpinner

    spinner = _SailingSpinner("Scanning", ascii_only=False)
    text = next(iter(spinner.__rich_console__(None, None))).plain
    assert "Scanning" in text
    assert any(frame in text for frame in spinner.frames)


def test_ascii_sailing_spinner_stays_ascii() -> None:
    from ferry.cli.ui import _SailingSpinner

    spinner = _SailingSpinner("Scanning", ascii_only=True)
    assert next(iter(spinner.__rich_console__(None, None))).plain.isascii()


# ---------- the animated banner ----------


def _terminal_ui(theme=HARBOR):
    """A UI whose console believes it is a colour terminal, writing to a buffer."""
    from rich.console import Console

    ui = UI(theme, capability=Capability.COLOR)
    buf = io.StringIO()
    ui.console = Console(
        file=buf, force_terminal=True, force_interactive=False, width=100, no_color=True
    )
    return ui, buf


def test_the_animated_banner_ends_with_the_whole_wordmark(monkeypatch) -> None:
    """Regression: the banner once animated to completion while drawing nothing,
    because the reveal width was measured from an empty row."""
    monkeypatch.setattr("time.sleep", lambda _s: None)
    ui, buf = _terminal_ui()
    ui.banner(animate=True)
    output = buf.getvalue()
    for row in build_wordmark(ui.theme).letter_rows:
        assert row in output, f"missing letterform row {row!r}"
    assert TAGLINE in output


def test_the_animated_banner_shows_the_wake_in_more_than_one_position(monkeypatch) -> None:
    """If the wake never moved, the mark would just be a static drawing."""
    from ferry.cli.brand import MARK_WIDTH
    from ferry.cli.motion import WAKE_PERIOD, wake_row

    monkeypatch.setattr("time.sleep", lambda _s: None)
    ui, buf = _terminal_ui()
    ui.banner(animate=True)
    output = buf.getvalue()
    seen = sum(1 for phase in range(WAKE_PERIOD) if wake_row(MARK_WIDTH, phase) in output)
    assert seen >= 2, "the wake never drifted"


def test_the_still_banner_draws_the_whole_wordmark_too() -> None:
    """Piped output gets no animation, but must not get a half-drawn name."""
    ui, buf = _terminal_ui()
    ui.banner(animate=False)
    output = buf.getvalue()
    for row in build_wordmark(ui.theme).letter_rows:
        assert row in output


def test_the_mono_banner_is_pure_ascii() -> None:
    ui, buf = _terminal_ui(MONO)
    ui.banner(animate=True)
    assert buf.getvalue().isascii()


def test_a_scan_shows_every_adapter_caveat(capsys) -> None:  # type: ignore[no-untyped-def]
    """A caveat is not a note. Notes are detail; a caveat is something the user
    must know before they trust an export, so it is printed on every scan.
    PLAN.md M5 requires it for Copilot Chat specifically.
    """
    from ferry.adapters.base import Adapter, DetectResult
    from ferry.cli.menu import scan
    from ferry.cli.theme import MONO, Capability
    from ferry.cli.ui import UI

    class _Caveated(Adapter):
        name = "demo"
        display_name = "Demo Tool"

        def detect(self) -> DetectResult:
            return DetectResult(installed=True, caveats=["the format is guesswork"])

        def export(self, dest_bundle_dir):  # type: ignore[no-untyped-def]
            yield from ()

        def import_(self, bundle_dir, options):  # type: ignore[no-untyped-def]
            yield from ()

    ui = UI(MONO, capability=Capability.PLAIN)
    buffer = io.StringIO()
    ui.console.file = buffer
    ui.console.width = 100

    scan(ui, [_Caveated()])

    assert "the format is guesswork" in buffer.getvalue()
    assert "Demo Tool" in buffer.getvalue()
