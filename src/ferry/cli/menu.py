"""The interactive flows: scan, top-level menu, and the per-action stubs.

Export and Import reach real adapters; Inspect and Compact are still stubs.
The wordmark, the
scan screen, the theme picker, the slash filter and the non-interactive guard
are all real, so later milestones fill in behaviour behind a finished interface.
"""

from __future__ import annotations

from ferry.adapters.base import Adapter, DetectResult, list_adapters
from ferry.cli.flows import run_export, run_import
from ferry.cli.motion import MENU_MOTION
from ferry.cli.theme import IconSet
from ferry.cli.ui import UI, NonInteractiveError, _DetectionRow
from ferry.config import write_setting

__all__ = ["MENU_ITEMS", "run_menu", "scan"]

MENU_ITEMS: list[tuple[str, str]] = [
    ("export", "Export conversations to a bundle"),
    ("import", "Import a bundle into a tool"),
    ("inspect", "Inspect a bundle"),
    ("compact", "Compact a conversation into a summary"),
    ("theme", "Change theme"),
    ("quit", "Quit"),
]
"""Top-level actions. Typing `/` filters this list, so the labels double as the
slash-command vocabulary — `/theme` finds "Change theme".

Each action has an animated icon in :data:`~ferry.cli.motion.MENU_MOTION`,
keyed by the same value.
"""

_MILESTONE_FOR_ACTION: dict[str, str] = {
    "inspect": "M7",
    "compact": "M7",
}
"""Actions that genuinely do not exist yet, and when they arrive."""

_WIRED = frozenset({"export", "import"})
"""Actions that reach a real adapter.

These were stubs reporting "arrives at M3" long after M3 and M4 had shipped --
two working adapters with no way to reach them. :mod:`ferry.cli.flows` is the
wiring; this set is what routes to it.
"""


def _describe(result: DetectResult, icons: IconSet) -> str:
    """One-line summary of what an adapter found, for the scan screen."""
    if not result.installed:
        return result.notes[0] if result.notes else "not found"
    count = result.conversation_count_estimate
    noun = "conversation" if count == 1 else "conversations"
    if result.version:
        return f"{result.version} {icons.separator} {count} {noun}"
    return f"{count} {noun}"


def scan(ui: UI, adapters: list[Adapter] | None = None) -> list[tuple[Adapter, DetectResult]]:
    """Detect every registered tool and render the results.

    ``detect()`` is contractually forbidden from raising, but this catches
    anyway: one misbehaving adapter must not stop the user reaching the others.
    """
    targets = list_adapters() if adapters is None else adapters
    results: list[tuple[Adapter, DetectResult]] = []

    with ui.scanning(f"Scanning for AI assistants{ui.theme.icons.ellipsis}"):
        for adapter in targets:
            try:
                result = adapter.detect()
            except Exception as exc:  # noqa: BLE001 - contract says never raise; trust nothing
                result = DetectResult(
                    installed=False, notes=[f"detection failed: {exc.__class__.__name__}"]
                )
            results.append((adapter, result))

    ui.blank()
    ui.detection_table(
        _DetectionRow(
            display_name=adapter.display_name,
            installed=result.installed,
            detail=_describe(result, ui.theme.icons),
        )
        for adapter, result in results
    )
    ui.blank()
    return results


def _stub(ui: UI, action: str) -> None:
    """Report that an action exists but has not been built yet."""
    milestone = _MILESTONE_FOR_ACTION.get(action, "a later milestone")
    ui.warn(f"Not implemented yet {ui.theme.icons.dash} arrives at {milestone}.")
    ui.blank()


def _change_theme(ui: UI) -> None:
    """Open the live-preview theme picker and apply the result.

    The new theme takes effect immediately and is saved, so the next run starts
    with it. A failed save is reported but not fatal — the choice still applies
    for this session.
    """
    from ferry.cli.themepicker import pick_theme

    chosen = pick_theme(ui.theme)
    if chosen is None:
        return

    ui.set_theme(chosen)
    if write_setting("theme", chosen):
        ui.success(f"Theme set to {chosen}.")
    else:
        dash = ui.theme.icons.dash
        ui.warn(f"Theme set to {chosen} for this session {dash} could not save it.")
    ui.blank()


def run_menu(ui: UI) -> int:
    """Show the wordmark, scan, then loop on the top-level menu.

    Returns:
        A process exit code.
    """
    ui.banner()

    try:
        results = scan(ui)
    except NonInteractiveError as exc:
        ui.error(str(exc))
        return 2

    if not any(result.installed for _, result in results):
        ui.info("No supported assistants detected on this machine yet.")
        ui.blank()

    while True:
        try:
            action = ui.select(
                "What would you like to do?",
                MENU_ITEMS,
                hint="Run `ferry --help` to see the non-interactive options.",
                motions=MENU_MOTION,
            )
        except NonInteractiveError as exc:
            ui.error(str(exc))
            return 2

        if action is None or action == "quit":
            return 0

        if action == "theme":
            _change_theme(ui)
            continue

        if action in _WIRED:
            # Re-scanned rather than reused: the user may have installed or
            # removed a tool since the menu opened, and acting on a stale answer
            # is how an export silently misses a whole assistant.
            if action == "export":
                run_export(ui, scan(ui))
            else:
                run_import(ui, scan(ui))
            continue

        _stub(ui, action)
