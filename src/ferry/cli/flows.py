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
from ferry.core.bundle import MANIFEST_NAME

__all__ = ["Scanned", "find_bundles", "run_export", "run_import"]

_MAX_SHOWN_WARNINGS = 8
"""Warnings printed in full before the rest are counted rather than listed."""

_TYPE_A_PATH = "\n type a path"
"""Sentinel choice value.

It holds a newline. Every other value in the list is ``str(absolute_path)``,
and no path can contain one, so this cannot be mistaken for a real directory.
"""

_MAX_LISTED_BUNDLES = 12
"""Enough to cover a working directory; past that the list stops being a list."""


Scanned = Sequence[tuple[Adapter, DetectResult]]
"""What :func:`ferry.cli.menu.scan` hands back."""


def _installed(adapters: Scanned) -> list[Adapter]:
    return [adapter for adapter, result in adapters if result.installed]


def _estimate(adapters: Scanned, chosen: Adapter) -> int:
    """How many conversations the scan thought this tool had.

    Only ever an estimate -- the scan counts files, the export reads them, and
    a file that turns out to be empty or unreadable never becomes a
    conversation. Used to size the bar, never to decide anything.
    """
    for adapter, result in adapters:
        if adapter is chosen:
            return result.conversation_count_estimate
    return 0


def _search_roots() -> list[Path]:
    """Where a bundle plausibly is, most likely first.

    The working directory comes first because that is where ``run_export``
    offers to put one. The rest are the three places a bundle copied from
    another machine actually lands.
    """
    home = Path.home()
    roots = [Path.cwd(), home / "Desktop", home / "Downloads", home / "Documents"]
    seen: set[Path] = set()
    unique: list[Path] = []
    for root in roots:
        if root not in seen:
            seen.add(root)
            unique.append(root)
    return unique


def find_bundles(roots: Sequence[Path] | None = None) -> list[Path]:
    """Directories that look like bundles, nearest first.

    Deliberately shallow -- a root and its immediate children. Walking a whole
    home directory to populate a menu would cost seconds and surprise the user
    for a list they will read in one glance.
    """
    found: list[Path] = []
    seen: set[Path] = set()
    for root in _search_roots() if roots is None else roots:
        try:
            if not root.is_dir():
                continue
            candidates = [root, *sorted(p for p in root.iterdir() if p.is_dir())]
        except OSError:
            # An unreadable or disconnected root is not worth a failure here.
            continue
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if (candidate / MANIFEST_NAME).is_file():
                found.append(candidate)
    return found


def _describe(path: Path) -> str:
    """A one-line label: what is in this bundle, and where it is."""
    try:
        manifest = Bundle.open(path).manifest
    except BundleError:
        return f"{path.name}  -  unreadable"
    tools = ", ".join(manifest.tools_included) or "nothing"
    plural = "" if manifest.conversation_count == 1 else "s"
    return (
        f"{path.name}  -  {manifest.conversation_count} conversation{plural}"
        f" from {tools}, {manifest.created_at:%d %b %Y}"
    )


def _choose_bundle(ui: UI) -> Path | None:
    """Pick a bundle by arrow key, falling back to typing a path.

    The first version of this screen asked for a path outright. With no
    default and no list, it presented a blank line and waited -- the user had
    no way to know what to enter, which is the same failure as asking someone
    to type an identifier.
    """
    bundles = find_bundles()
    if not bundles:
        ui.info("No bundle found nearby - looked in this folder, Desktop, Downloads, Documents.")
        return _typed_bundle(ui)

    listed = bundles[:_MAX_LISTED_BUNDLES]
    choices = [(str(p), _describe(p)) for p in listed]
    choices.append((_TYPE_A_PATH, "Somewhere else - type the path"))
    chosen = ui.select("Which bundle should be imported?", choices, hint="use --bundle")
    if chosen is None:
        return None
    if chosen == _TYPE_A_PATH:
        return _typed_bundle(ui)
    return Path(chosen)


def _typed_bundle(ui: UI) -> Path | None:
    ui.detail("Type or paste the folder holding the bundle. Tab completes it.")
    answer = ui.path("Path to the bundle", hint="use --bundle")
    return Path(answer).expanduser() if answer else None


def _default_bundle_name() -> str:
    return f"ferry-bundle-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


_MAX_LABEL = 46
"""How much of a conversation's description fits beside the bar."""


def _shorten(text: str) -> str:
    """Trim a label so the bar and counter are never pushed off the line.

    The ellipsis counts toward the budget. Trimming to ``_MAX_LABEL`` and then
    appending it produces a longer string than the limit it was meant to keep.
    """
    if len(text) <= _MAX_LABEL:
        return text
    return text[: _MAX_LABEL - 3].rstrip() + "..."


def _report(
    ui: UI,
    events: Iterator[ExportEvent | ImportEvent],
    noun: str,
    *,
    label: str,
    total: int,
) -> int:
    """Render an adapter's event stream. Returns the number of conversations handled.

    The adapters are generators, so this is also what drives them -- the work
    happens as the events are consumed, which is what lets the bar advance in
    step with it rather than after the fact.

    ``total`` is an estimate, not a promise. It comes from ``detect()`` for an
    export and from the manifest for an import, and both can be out by the time
    the work runs; :meth:`UI.progress` raises its own total rather than sitting
    full while work continues.

    Warnings and errors are collected and printed *after* the bar rather than
    during it. A warning scrolling past under a live bar is a warning nobody
    reads, and the whole point of showing them is that they are read.
    """
    kinds: Counter[str] = Counter()
    warnings: list[str] = []
    errors: list[str] = []
    handled = 0

    with ui.progress(label, total) as bar:
        for event in events:
            kinds[event.kind] += 1
            if event.kind == "started":
                bar.describe(_shorten(event.message))
            elif event.kind == "progress":
                handled += 1
                bar.advance()
                bar.describe(_shorten(event.message))
            elif event.kind == "skipped":
                bar.advance()
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

        ui.detail("A new folder will be made here. Enter accepts the suggestion below.")
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
    _report(
        ui,
        adapter.export(target),
        "conversations exported",
        label="Exporting",
        total=_estimate(adapters, adapter),
    )
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
        picked = _choose_bundle(ui)
        if picked is None:
            return
        bundle_dir = picked

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
    _report(
        ui,
        adapter.import_(bundle_dir, ImportOptions()),
        "conversations imported",
        label="Importing",
        total=count,
    )
