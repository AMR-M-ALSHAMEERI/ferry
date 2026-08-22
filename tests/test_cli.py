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
from ferry.cli.menu import _describe, scan
from ferry.cli.theme import HARBOR, MONO, Capability
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


def test_stub_adapters_report_not_installed_with_a_reason() -> None:
    """M2 stubs must be honest — no invented conversation counts."""
    for adapter in list_adapters():
        result = adapter.detect()
        assert result.installed is False
        assert result.conversation_count_estimate == 0
        assert result.notes, f"{adapter.name} gave no reason"
        assert "not yet implemented" in result.notes[0]


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
    assert _describe(r) == "1 conversation"


def test_describe_uses_plural_otherwise() -> None:
    r = DetectResult(installed=True, conversation_count_estimate=4)
    assert _describe(r) == "4 conversations"


def test_describe_includes_version_when_known() -> None:
    r = DetectResult(installed=True, version="2.1.0", conversation_count_estimate=2)
    assert _describe(r) == "2.1.0 · 2 conversations"


def test_describe_surfaces_the_reason_when_not_installed() -> None:
    r = DetectResult(installed=False, notes=["adapter not yet implemented"])
    assert _describe(r) == "adapter not yet implemented"


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

    def select(self, question, choices, *, hint=""):  # type: ignore[override]
        self.asked += 1
        return self._answers.pop(0)


def _menu_output(ui: UI) -> str:
    buf = io.StringIO()
    ui.console.file = buf
    return buf.getvalue()


def test_menu_quits_cleanly_and_returns_zero() -> None:
    from ferry.cli.menu import run_menu

    ui = _ScriptedUI(["quit"])
    assert run_menu(ui) == 0
    assert ui.asked == 1


def test_menu_treats_cancel_as_quit() -> None:
    """questionary returns None when the user presses Ctrl+C."""
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
