"""The Export and Import screens.

Two working adapters existed for a while with no way to reach them from the
interface, and the menu still told anyone who picked Export that it would
"arrive at M3" -- a milestone that had shipped. This module is that wiring, and
nothing more: it picks a tool, picks a place, runs the adapter and renders the
events it yields.

It deliberately does **not** add features. Dry-run, conflict resolution,
encryption and ``ferry inspect`` are M7; the adapters already accept the
options, and this screen simply uses the safe defaults. The one thing it must
get right is that a person can see what is about to happen and stop it.

Every event an adapter yields is shown. An adapter that reports 12 warnings
about what it could not carry is telling the user something they need, and a
screen that swallows those in favour of a tidy progress bar would be lying by
omission.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

from ferry.adapters.base import (
    Adapter,
    DetectResult,
    ExportEvent,
    ImportEvent,
    ImportOptions,
)
from ferry.cli.ui import UI, NonInteractiveError
from ferry.core import Bundle, BundleError

__all__ = ["Scanned", "run_export", "run_import"]

_MAX_SHOWN_WARNINGS = 8
"""Warnings printed in full before the rest are counted rather than listed."""


Scanned = Sequence[tuple[Adapter, DetectResult]]
"""What :func:`ferry.cli.menu.scan` hands back."""


def _installed(adapters: Scanned) -> list[Adapter]:
    return [adapter for adapter, result in adapters if result.installed]


def _default_bundle_name() -> str:
    return f"ferry-bundle-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def _report(ui: UI, events: Iterator[ExportEvent | ImportEvent], noun: str) -> int:
    """Render an adapter's event stream. Returns the number of conversations handled.

    The adapters are generators, so this is also what drives them -- the work
    happens as the events are consumed.
    """
    kinds: Counter[str] = Counter()
    warnings: list[str] = []
    errors: list[str] = []
    handled = 0

    for event in events:
        kinds[event.kind] += 1
        if event.kind == "started":
            ui.info(event.message)
        elif event.kind == "progress":
            handled += 1
            ui.detail(event.message)
        elif event.kind == "warning":
            warnings.append(event.message)
        elif event.kind == "error":
            errors.append(event.message)

    ui.blank()
    for message in errors:
        ui.error(message)

    shown = warnings[:_MAX_SHOWN_WARNINGS]
    for message in shown:
        ui.warn(message)
    if len(warnings) > len(shown):
        ui.warn(f"... and {len(warnings) - len(shown)} more")

    skipped = kinds.get("skipped", 0)
    summary = f"{handled} {noun}"
    if skipped:
        summary += f", {skipped} skipped"
    if errors:
        ui.error(f"{summary}, {len(errors)} failed")
    else:
        ui.success(summary)
    ui.blank()
    return handled


def run_export(ui: UI, adapters: Scanned) -> None:
    """Pick a tool, pick a destination, export."""
    available = _installed(adapters)
    if not available:
        ui.info("Nothing to export - no supported assistant was found on this machine.")
        ui.blank()
        return

    try:
        if len(available) == 1:
            adapter = available[0]
            ui.info(f"Exporting from {adapter.display_name}, the only assistant found.")
        else:
            chosen = ui.select(
                "Export from which assistant?",
                [(a.name, a.display_name) for a in available],
                hint="use --tool",
            )
            if chosen is None:
                return
            adapter = next(a for a in available if a.name == chosen)

        destination = ui.path(
            "Where should the bundle go?",
            default=str(Path.cwd() / _default_bundle_name()),
            hint="use --output",
        )
        if not destination:
            return
        target = Path(destination).expanduser()

        if target.exists() and any(target.iterdir()):
            # Re-using a bundle directory is how an interrupted export resumes,
            # so this is a question rather than a refusal.
            resume = ui.confirm(
                f"{target.name} already has something in it. Add to it?",
                default=True,
                hint="use --force",
            )
            if not resume:
                return
    except NonInteractiveError as exc:
        ui.error(str(exc))
        return

    ui.blank()
    _report(ui, adapter.export(target), "conversations exported")
    ui.info(f"Bundle: {target}")
    ui.blank()


def run_import(ui: UI, adapters: Scanned) -> None:
    """Pick a bundle, pick a target, confirm, import."""
    available = _installed(adapters)
    if not available:
        ui.info("Nothing to import into - no supported assistant was found on this machine.")
        ui.blank()
        return

    try:
        source = ui.path("Which bundle should be imported?", hint="use --bundle")
        if not source:
            return
        bundle_dir = Path(source).expanduser()

        try:
            bundle = Bundle.open(bundle_dir)
        except BundleError as exc:
            ui.error(str(exc))
            ui.blank()
            return

        count = len(bundle.list_conversations())
        tools = ", ".join(bundle.manifest.tools_included) or "nothing"
        ui.info(f"{count} conversations, from {tools}, made by {bundle.manifest.created_by}")

        if len(available) == 1:
            adapter = available[0]
            ui.info(f"Importing into {adapter.display_name}, the only assistant found.")
        else:
            chosen = ui.select(
                "Import into which assistant?",
                [(a.name, a.display_name) for a in available],
                hint="use --tool",
            )
            if chosen is None:
                return
            adapter = next(a for a in available if a.name == chosen)

        # The only screen in Ferry that writes into a user's real conversation
        # history. It says so, and it defaults to no.
        ui.blank()
        ui.warn(f"This writes into your real {adapter.display_name} history.")
        ui.info("Existing conversations are kept: anything already there is skipped.")
        if not ui.confirm(f"Import {count} conversations?", default=False, hint="use --yes"):
            ui.info("Nothing was written.")
            ui.blank()
            return
    except NonInteractiveError as exc:
        ui.error(str(exc))
        return

    ui.blank()
    _report(ui, adapter.import_(bundle_dir, ImportOptions()), "conversations imported")
