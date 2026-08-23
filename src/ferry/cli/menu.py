"""The interactive flows: scan, top-level menu, and the per-action stubs.

At M2 the export/import/inspect/compact actions are stubs. The wordmark, the
scan screen, the theme picker, the slash filter and the non-interactive guard
are all real, so later milestones fill in behaviour behind a finished interface.
"""

from __future__ import annotations

from ferry.adapters.base import Adapter, DetectResult, list_adapters
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

_BUILT_BUT_UNWIRED = frozenset({"export", "import"})
"""Actions whose adapters work but which the menu cannot reach yet.

Export and Import used to be listed above as "arrives at M3". M3 shipped, and
so did M4, and the message went on claiming otherwise -- which is worse than
saying nothing, because it tells someone a finished thing is unfinished. The
adapters are tested against real data; only this wiring is missing.
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
    """Say accurately why an action did nothing."""
    if action in _BUILT_BUT_UNWIRED:
        dash = ui.theme.icons.dash
        ui.warn(f"Not available from the menu yet {dash} the adapters work, this screen does not.")
    else:
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

        _stub(ui, action)
