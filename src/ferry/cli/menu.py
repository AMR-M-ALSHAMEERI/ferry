"""The interactive flows: scan, top-level menu, and the per-action stubs.

At M2 the actions are stubs. The menu structure, the scan screen and the
non-interactive guard are real, so later milestones fill in behaviour behind an
interface that already works.
"""

from __future__ import annotations

from ferry.adapters.base import Adapter, DetectResult, list_adapters
from ferry.cli.ui import UI, NonInteractiveError, _DetectionRow

__all__ = ["run_menu", "scan"]

_MENU_CHOICES: list[tuple[str, str]] = [
    ("export", "Export conversations to a bundle"),
    ("import", "Import a bundle into a tool"),
    ("inspect", "Inspect a bundle"),
    ("compact", "Compact a conversation into a summary"),
    ("quit", "Quit"),
]

_MILESTONE_FOR_ACTION: dict[str, str] = {
    "export": "M3",
    "import": "M3",
    "inspect": "M7",
    "compact": "M7",
}


def _describe(result: DetectResult) -> str:
    """One-line summary of what an adapter found, for the scan screen."""
    if not result.installed:
        return result.notes[0] if result.notes else "not found"
    count = result.conversation_count_estimate
    noun = "conversation" if count == 1 else "conversations"
    if result.version:
        return f"{result.version} · {count} {noun}"
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
            detail=_describe(result),
        )
        for adapter, result in results
    )
    ui.blank()
    return results


def _stub(ui: UI, action: str) -> None:
    """Report that an action exists but has not been built yet."""
    milestone = _MILESTONE_FOR_ACTION.get(action, "a later milestone")
    ui.warn(f"Not implemented yet — arrives at {milestone}.")
    ui.blank()


def run_menu(ui: UI) -> int:
    """Run the scan, then loop on the top-level menu.

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
                _MENU_CHOICES,
                hint="Run `ferry --help` to see the non-interactive options.",
            )
        except NonInteractiveError as exc:
            ui.error(str(exc))
            return 2

        if action is None or action == "quit":
            return 0

        _stub(ui, action)
