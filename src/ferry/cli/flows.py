"""The Export and Import screens.

Two working adapters existed for a while with no way to reach them from the
interface, and the menu still told anyone who picked Export that it would
"arrive at M3" -- a milestone that had shipped. This module is that wiring, and
nothing more: it picks a tool, picks a place, runs the adapter and renders the
events it yields.

The one thing it must get right is that a person can see what is about to
happen and stop it. That is why the import screen offers a preview before it
offers to write, and why the inspect screen shows a bundle in full before it
offers to delete anything from it.

Every event an adapter yields is shown. An adapter that reports 12 warnings
about what it could not carry is telling the user something they need, and a
screen that swallows those in favour of a tidy progress bar would be lying by
omission.
"""

from __future__ import annotations

import shutil
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from ferry.adapters.base import (
    Adapter,
    DetectResult,
    ExportEvent,
    ImportEvent,
    ImportOptions,
    OnConflict,
)
from ferry.adapters.census import count_of
from ferry.cli.ui import UI, NonInteractiveError
from ferry.core import Bundle, BundleError, BundleSummary, delete_bundle, summarise
from ferry.core.bundle import MANIFEST_NAME
from ferry.core.crypto import WrongPassphrase
from ferry.core.sealed import (
    SEALED_SUFFIX,
    is_sealed,
    opens_with,
    seal_bundle,
    unsealed,
)
from ferry.core.summary import ConversationSummary

__all__ = ["Scanned", "find_bundles", "run_export", "run_import", "run_inspect"]

_MAX_SHOWN_WARNINGS = 8
"""Messages printed in full, per group, before the rest are counted."""

_TYPE_A_PATH = "\n type a path"
"""Sentinel choice value.

It holds a newline. Every other value in the list is ``str(absolute_path)``,
and no path can contain one, so this cannot be mistaken for a real directory.
"""

_MAX_LISTED_FOLDERS = 10
"""Enough to see the shape of a bundle without turning the screen into a list."""

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
        # Sealed bundles are files, not directories, so the directory walk
        # above cannot see them. A bundle someone encrypted must still appear
        # in the picker, or encrypting one hides it from Ferry.
        try:
            for candidate in sorted(root.glob(f"*{SEALED_SUFFIX}")):
                resolved = candidate.resolve()
                if resolved not in seen and candidate.is_file() and is_sealed(candidate):
                    seen.add(resolved)
                    found.append(candidate)
        except OSError:
            continue
    return found


def _describe(path: Path) -> str:
    """A one-line label: what is in this bundle, and where it is.

    A sealed bundle can only be described by its size and date -- everything
    else about it is encrypted, which is the point.
    """
    if path.is_file():
        if not is_sealed(path):
            return f"{path.name}  -  not a bundle"
        made = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        return (
            f"{path.name}  -  sealed, {path.stat().st_size / 1024 / 1024:.1f} MB, {made:%d %b %Y}"
        )
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


def _choose_bundle(ui: UI, question: str = "Which bundle should be imported?") -> Path | None:
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
    chosen = ui.select(question, choices, hint="use --bundle")
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


def _with(
    first: ExportEvent | ImportEvent | None,
    rest: Iterator[ExportEvent | ImportEvent],
) -> Iterator[ExportEvent | ImportEvent]:
    """The event that was read early, followed by the others."""
    if first is not None:
        yield first
    yield from rest


def _print_group(
    ui: UI,
    heading: str,
    messages: list[str],
    write: Callable[[str], None],
) -> None:
    """One block of messages under its own heading, or nothing at all.

    Silent when there is nothing to say -- an empty "Warnings" heading reads
    like something is missing.
    """
    if not messages:
        return

    # The same sentence repeated once per conversation is one piece of
    # information. Adapters that describe what a rebuild costs emit it for
    # every conversation they rebuild; the user needs to read it once.
    repeats: Counter[str] = Counter(messages)
    unique = list(dict.fromkeys(messages))

    ui.blank()
    ui.info(heading)
    shown = unique[:_MAX_SHOWN_WARNINGS]
    for message in shown:
        count = repeats[message]
        write(f"{message} (x{count})" if count > 1 else message)
    if len(unique) > len(shown):
        write(f"... and {len(unique) - len(shown)} more")


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

    Notes, warnings and errors are collected and printed *after* the bar rather
    than during it. A message scrolling past under a live bar is one nobody
    reads, and the whole point of showing them is that they are read.

    They are then printed **grouped by severity, under headings**. Before that
    they came out as one undifferentiated list, every line carrying the warning
    marker, which made a routine remark about the storage format look like
    something had gone wrong.
    """
    kinds: Counter[str] = Counter()
    notes: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []
    summary_line = ""
    handled = 0

    # The first event is read before the bar is drawn, because a ``started``
    # event may carry the real amount of work. Sizing the bar from detect()
    # alone made it read "6/2" for an Antigravity export: two conversations,
    # but six things to write.
    opening = next(events, None)
    if opening is not None and opening.total:
        total = opening.total

    with ui.progress(label, total) as bar:
        for event in _with(opening, events):
            kinds[event.kind] += 1
            if event.kind == "started":
                bar.describe(_shorten(event.message))
            elif event.kind == "progress":
                handled += 1
                bar.advance()
                bar.describe(_shorten(event.message))
            elif event.kind == "skipped":
                bar.advance()
            elif event.kind == "note":
                notes.append(event.message)
            elif event.kind == "warning":
                warnings.append(event.message)
            elif event.kind == "error":
                errors.append(event.message)
            elif event.kind == "done":
                summary_line = event.message

    ui.blank()
    for message in errors:
        ui.error(message)

    _print_group(ui, "Notes", notes, ui.detail)
    _print_group(ui, "Warnings", warnings, ui.warn)

    # The adapter's own closing line wins when it wrote one. It knows things
    # the screen cannot work out from counting events -- that four of six
    # writes were subagent trajectories rather than conversations, say.
    skipped = kinds.get("skipped", 0)
    summary = summary_line or f"{handled} {noun}"
    if skipped and not summary_line:
        summary += f", {skipped} skipped"
    if errors:
        ui.error(f"{summary}, {len(errors)} failed")
    else:
        ui.success(summary)
    ui.blank()
    return handled


_ACTIONS: list[tuple[str, str]] = [
    ("preview", "Preview it first - nothing is written"),
    ("skip", "Import, leaving anything already there alone"),
    ("rename", "Import, keeping both copies of anything already there"),
    ("overwrite", "Import, replacing what is there - the old copy is backed up"),
    ("cancel", "Cancel"),
]
"""One screen instead of three.

The options an import takes -- preview or write, and what to do about a
conversation already there -- were separate questions in the first draft of
this screen. Asked separately they are three prompts standing between someone
and a routine restore, and two of them are about a situation that may not
arise. Asked as one sentence they are a single choice with a safe default under
the cursor.
"""


def _conflict(action: str) -> OnConflict:
    """The chosen action as the option the adapters take.

    A lookup rather than a cast: if a choice is ever added to ``_ACTIONS``
    without a matching behaviour, this fails loudly here instead of reaching an
    adapter as an unrecognised string and being treated as "overwrite" by
    whichever branch happens to fall through.
    """
    if action not in ("skip", "rename", "overwrite"):
        raise ValueError(f"no import behaviour for {action!r}")
    return cast("OnConflict", action)


def _recorded_home(bundle: Bundle) -> str | None:
    """The home directory of the machine the bundle came from.

    Read from the manifest rather than from the conversations. Every adapter
    records an absolute working directory per conversation, so the complete
    answer means opening every conversation file -- and one Codex conversation
    is 53 MB. The manifest's home covers the case this exists for, which is a
    bundle restored onto a different machine or under a different username;
    ``ferry inspect`` is where the full list of recorded folders belongs.
    """
    home = bundle.manifest.source_machine.user_home
    return home or None


def _path_remap(ui: UI, bundle: Bundle) -> tuple[tuple[str, str], ...] | None:
    """Ask where the bundle's folders live on this machine, if they have moved.

    Silent when the recorded home is this machine's home, or when it still
    exists here -- the overwhelmingly common case is restoring onto the machine
    the bundle came from, and a question with one sensible answer is not worth
    asking.

    Returns ``None`` if the user cancelled, ``()`` if nothing needs remapping.
    """
    recorded = _recorded_home(bundle)
    if not recorded:
        return ()
    here = str(Path.home())
    if recorded == here or Path(recorded).is_dir():
        return ()

    ui.blank()
    ui.warn(f"This bundle was made on a machine whose home folder was {recorded}.")
    ui.info("That folder is not on this machine, so the paths inside would be wrong.")
    chosen = ui.select(
        "Where should those folders be read as now?",
        [
            ("here", f"{here} - this machine's home folder"),
            ("other", "Somewhere else - choose a folder"),
            ("leave", "Leave them as they are"),
        ],
        hint="use --path-remap",
    )
    if chosen is None:
        return None
    if chosen == "leave":
        return ()
    if chosen == "here":
        return ((recorded, here),)

    typed = ui.path("Which folder?", default=here, hint="use --path-remap")
    if not typed:
        return None
    return ((recorded, str(Path(typed).expanduser())),)


# --------------------------------------------------------------------------
# sealing
# --------------------------------------------------------------------------


def _ask_new_passphrase(ui: UI) -> str | None:
    """Ask for a passphrase twice, and say what is at stake before asking once.

    Confirmed rather than trusted because there is no recovery: a passphrase
    mistyped once and never noticed produces a file nobody can ever open, and
    the person finds out on the day they need it.
    """
    ui.blank()
    ui.warn("There is no way to recover a sealed bundle without its passphrase.")
    ui.info("Not by Ferry, not by anyone. Lose it and the backup is gone.")
    ui.detail("Sealing protects the bundle you carry or store, not the machine it was made on.")

    first = ui.secret("Passphrase", hint="use --passphrase")
    if not first:
        return None
    again = ui.secret("Again, to be sure", hint="use --passphrase")
    if not again:
        return None
    if first != again:
        ui.error("Those did not match. Nothing was sealed.")
        ui.blank()
        return None
    return first


def _offer_to_seal(ui: UI, target: Path) -> None:
    """After an export: encrypt the bundle into one file, if asked.

    The plaintext bundle is only removed **after** the sealed file has been
    opened again with the same passphrase. Deleting first and verifying later
    is how a backup tool destroys a backup.
    """
    try:
        if not ui.confirm(
            "Encrypt this bundle into a single file?", default=False, hint="use --encrypt"
        ):
            return
        passphrase = _ask_new_passphrase(ui)
        if passphrase is None:
            ui.info("Left unencrypted.")
            ui.blank()
            return
    except NonInteractiveError as exc:
        ui.error(str(exc))
        return

    try:
        with ui.scanning("Sealing"):
            sealed = seal_bundle(target, passphrase)
    except (BundleError, OSError) as exc:
        ui.error(f"could not seal the bundle: {exc}")
        ui.blank()
        return

    with ui.scanning("Checking it opens"):
        confirmed = opens_with(sealed.path, passphrase)
    if not confirmed:
        # Should be impossible, and is checked anyway: this is the one place
        # where being wrong costs the user everything.
        ui.error(
            "The sealed file did not open with that passphrase. Your bundle was left as it is."
        )
        ui.blank()
        return

    ui.success(f"Sealed: {sealed.path}  ({sealed.bytes_written / 1024 / 1024:.1f} MB)")

    try:
        remove = ui.confirm("Delete the unencrypted copy?", default=False, hint="use --replace")
    except NonInteractiveError:
        remove = False
    if remove:
        try:
            shutil.rmtree(target)
            ui.info(f"Removed {target.name}. The sealed file is now the only copy.")
        except OSError as exc:
            ui.error(f"could not remove {target}: {exc}")
    else:
        ui.detail(f"The unencrypted bundle is still at {target}")
    ui.blank()


PASSPHRASE_TRIES: Final = 3
"""Attempts before the screen gives up.

Three because a mistyped passphrase is the ordinary case and sending someone
back to the main menu over one slip is hostile -- but a prompt that never stops
asking is a prompt nobody can leave. Escape gets out at any point and does not
spend an attempt.

There is no lockout and no delay beyond deriving the key, which already costs
about a quarter of a second. That is the rate limit, and it applies to someone
with the file and a script exactly as it applies here.
"""


def _passphrase_refused(ui: UI, attempt: int) -> None:
    """Say a passphrase did not work, without claiming to know why.

    Ferry cannot tell a mistyped passphrase from an altered file -- the
    authentication tag fails identically for both, deliberately. So the first
    tries say the plain thing, and the last one names **both** possibilities
    rather than sending someone to hunt for a passphrase that was right all
    along.
    """
    left = PASSPHRASE_TRIES - attempt
    if left > 0:
        ui.error(f"That did not open it. {count_of(left, 'try', 'tries')} left.")
        return
    ui.error("That did not open it.")
    ui.detail(
        "Either the passphrase is not the right one, or the file has been "
        "altered since it was sealed. Ferry cannot tell those apart."
    )
    ui.blank()


@contextmanager
def _opened(ui: UI, picked: Path) -> Iterator[Path | None]:
    """Yield a directory holding the bundle, unsealing it first if it is sealed.

    Yields ``None`` when the user backed out of the passphrase or it was wrong,
    so every caller has one shape to handle. The unsealed copy is removed on
    the way out, including when the caller raises.
    """
    if not is_sealed(picked):
        yield picked
        return

    for attempt in range(1, PASSPHRASE_TRIES + 1):
        try:
            passphrase = ui.secret(f"Passphrase for {picked.name}", hint="use --passphrase")
        except NonInteractiveError as exc:
            ui.error(str(exc))
            yield None
            return
        if not passphrase:
            # Escape, or an empty line. Backing out is not a failed attempt and
            # must not be spent as one -- the way out of this prompt has to work
            # on the first press, every time.
            yield None
            return

        try:
            # The spinner covers the unsealing and **stops there**. Written as
            # one `with` alongside `unsealed`, it stayed alive across the
            # `yield` -- so "Opening" kept spinning underneath the import
            # screen, the conflict question and the whole inspect listing,
            # long after the bundle was open. The two need different
            # lifetimes: the spinner ends when the work it describes ends, the
            # unsealed copy lives until the caller is finished with it.
            with ExitStack() as opened:
                with ui.scanning("Opening"):
                    bundle = opened.enter_context(unsealed(picked, passphrase))
                yield bundle.root
            return
        except WrongPassphrase:
            _passphrase_refused(ui, attempt)
        except (BundleError, OSError) as exc:
            ui.error(str(exc))
            ui.blank()
            yield None
            return

    yield None


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
    _offer_to_seal(ui, target)


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
    except NonInteractiveError as exc:
        ui.error(str(exc))
        return

    # A sealed bundle is opened into a temporary folder that exists only for
    # as long as this import does, so everything below reads an ordinary
    # bundle and knows nothing about encryption.
    with _opened(ui, picked) as bundle_dir:
        if bundle_dir is None:
            return
        _import_from(ui, available, bundle_dir)


def _import_from(ui: UI, available: list[Adapter], bundle_dir: Path) -> None:
    """Everything after a bundle has been chosen and, if sealed, opened."""
    try:
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

        remap = _path_remap(ui, bundle)
        if remap is None:
            return

        # The only screen in Ferry that writes into a user's real conversation
        # history. It says so, and the option under the cursor is the one that
        # writes nothing.
        ui.blank()
        ui.warn(f"This writes into your real {adapter.display_name} history.")
        action = ui.select(
            f"Import {count} conversations?",
            _ACTIONS,
            hint="use --dry-run / --on-conflict",
        )
        if action is None or action == "cancel":
            ui.info("Nothing was written.")
            ui.blank()
            return
    except NonInteractiveError as exc:
        ui.error(str(exc))
        return

    if action == "preview":
        ui.blank()
        ui.info("Preview only. Nothing below is written.")
        _report(
            ui,
            adapter.import_(bundle_dir, ImportOptions(dry_run=True, path_remap=remap)),
            "conversations would be imported",
            label="Previewing",
            total=count,
        )
        try:
            action = ui.select(
                "Import for real?",
                [choice for choice in _ACTIONS if choice[0] != "preview"],
                hint="use --on-conflict",
            )
        except NonInteractiveError as exc:
            ui.error(str(exc))
            return
        if action is None or action == "cancel":
            ui.info("Nothing was written.")
            ui.blank()
            return

    ui.blank()
    _report(
        ui,
        adapter.import_(bundle_dir, ImportOptions(on_conflict=_conflict(action), path_remap=remap)),
        "conversations imported",
        label="Importing",
        total=count,
    )


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------


def _megabytes(count: int) -> str:
    """A size someone can read at a glance, never a bare byte count.

    Under a megabyte reports as "under 1 MB" rather than in kilobytes. These
    numbers sit in a column beside multi-gigabyte databases, and mixing units
    down one column makes them harder to compare, not easier.
    """
    if count < 1024 * 1024:
        return "under 1 MB"
    return f"{count / 1024 / 1024:,.1f} MB"


def _when(summary: ConversationSummary) -> str:
    moment = summary.updated_at or summary.created_at
    return f"{moment:%d %b %Y}" if moment else "undated"


_MAX_TITLE = 48
"""How much of a conversation title fits before the facts about it.

Titles are whatever the tool made of the first message, and Codex routinely
makes that the entire paragraph. Trimming the **line** put the title first and
pushed the message count, the date and the size off the end -- so every row
read as a wall of prose with none of the information needed to choose between
them. The title is the variable part, so the title is what gets cut.
"""


def _conversation_line(summary: ConversationSummary) -> str:
    name = _clip(summary.name, _MAX_TITLE)
    if summary.unreadable:
        return f"{name}  -  cannot be read: {_clip(summary.unreadable, 40)}"
    parts = [count_of(summary.messages, "message"), _when(summary)]
    if summary.attachments:
        parts.append(count_of(summary.attachments, "attachment"))
    if summary.has_source_raw:
        parts.append("original kept")
    parts.append(_megabytes(summary.bytes_on_disk))
    return f"{name:<{_MAX_TITLE}}  {', '.join(parts)}"


def _clip(text: str, limit: int) -> str:
    """Trim to ``limit`` *including* the ellipsis.

    Trimming to the limit and then appending three dots produces a string
    longer than the limit it was meant to keep -- the same fault :func:`_shorten`
    was written to avoid for the progress bar.
    """
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def _show(ui: UI, summary: BundleSummary) -> None:
    """Print the whole bundle: what it holds, where it came from, what is wrong."""
    manifest = summary.manifest
    ui.blank()
    ui.info(f"{summary.root.name}  -  {_megabytes(summary.bytes_on_disk)} on disk")
    ui.detail(f"made {manifest.created_at:%d %b %Y at %H:%M} by {manifest.created_by}")
    machine = manifest.source_machine
    ui.detail(
        f"from {machine.hostname or 'an unnamed machine'} ({machine.os}), "
        f"home folder {machine.user_home}"
    )
    if manifest.encrypted:
        ui.detail(f"encrypted with {manifest.encryption_algo or 'an unrecorded algorithm'}")

    ui.blank()
    if not summary.conversations:
        ui.info("No conversations in this bundle.")
    else:
        by_tool = ", ".join(
            f"{count} from {tool}" for tool, count in sorted(summary.by_tool.items())
        )
        ui.info(f"{count_of(len(summary.conversations), 'conversation')}: {by_tool}")
        for conversation in summary.conversations:
            ui.detail(_conversation_line(conversation))

    folders = summary.folders
    if folders:
        ui.blank()
        # The list the import screen cannot afford to compute. Someone
        # restoring onto another machine needs to see which folders a bundle
        # expects before being asked where those folders now live.
        ui.info(count_of(len(folders), "folder") + " these conversations were recorded in:")
        for folder in folders[:_MAX_LISTED_FOLDERS]:
            ui.detail(folder)
        if len(folders) > _MAX_LISTED_FOLDERS:
            ui.detail(f"... and {len(folders) - _MAX_LISTED_FOLDERS} more")

    if summary.problems:
        ui.blank()
        ui.warn(count_of(len(summary.problems), "problem") + " with this bundle:")
        for problem in summary.problems[:_MAX_SHOWN_WARNINGS]:
            ui.detail(problem)
        if len(summary.problems) > _MAX_SHOWN_WARNINGS:
            ui.detail(f"... and {len(summary.problems) - _MAX_SHOWN_WARNINGS} more")
    ui.blank()


def run_inspect(ui: UI) -> None:
    """Look inside a bundle, and delete from it if that is what you came to do.

    Deleting lives on this screen rather than one of its own because it is the
    same act: you remove something after looking at it, never before. A
    standalone delete menu item would be a screen whose first job is to
    describe what you are about to lose -- which is this screen.
    """
    try:
        picked = _choose_bundle(ui, "Which bundle would you like to look inside?")
    except NonInteractiveError as exc:
        ui.error(str(exc))
        return
    if picked is None:
        return

    sealed = is_sealed(picked)
    with _opened(ui, picked) as opened:
        if opened is None:
            return
        if sealed:
            # Deleting from inside a sealed bundle would mean unsealing,
            # editing and resealing -- three chances to lose the only copy of
            # something, for a screen whose job is to let you look.
            ui.detail("Opened read-only. To change a sealed bundle, unseal it first.")
        _inspect_at(ui, opened, read_only=sealed)


def _inspect_at(ui: UI, picked: Path, *, read_only: bool = False) -> None:
    """The inspect screen, on a directory that is already a plain bundle."""
    while True:
        try:
            bundle = Bundle.open(picked)
        except BundleError as exc:
            ui.error(str(exc))
            ui.blank()
            return

        with ui.scanning(f"Reading {picked.name}"):
            summary = summarise(bundle)
        _show(ui, summary)

        choices = [("done", "Done")]
        if not read_only:
            if summary.conversations:
                choices.append(("one", "Delete one conversation from this bundle"))
            choices.append(("all", "Delete this whole bundle"))
        try:
            action = ui.select("Anything else?", choices, hint="inspect is read-only")
        except NonInteractiveError as exc:
            ui.error(str(exc))
            return

        if action is None or action == "done":
            return
        if action == "all":
            if _delete_bundle(ui, summary):
                return
            continue
        _delete_conversation(ui, bundle, summary)


def _delete_conversation(ui: UI, bundle: Bundle, summary: BundleSummary) -> None:
    """Pick one conversation and remove it, its attachments and its original file."""
    try:
        chosen = ui.select(
            "Which conversation should go?",
            [
                (str(conversation.id), _conversation_line(conversation))
                for conversation in summary.conversations
            ],
            hint="inspect is read-only",
        )
        if chosen is None:
            return
        doomed = next(c for c in summary.conversations if str(c.id) == chosen)

        # Named, not counted. This is the one action in Ferry after which the
        # data is simply gone -- a bundle *is* the backup -- so the question
        # says what is being lost instead of asking for a yes about "it".
        ui.blank()
        ui.warn(f"Deleting: {doomed.name}")
        ui.detail(
            f"{count_of(doomed.messages, 'message')}, {_when(doomed)}, "
            f"{_megabytes(doomed.bytes_on_disk)}, from {doomed.tool or 'an unknown tool'}"
        )
        ui.detail(
            f"{count_of(len(bundle.conversation_files(doomed.id)), 'file')} will be removed: "
            "the conversation, its attachments and the original file it came from"
        )
        ui.info("A copy goes to ~/.ferry/backups first.")
        if not ui.confirm("Delete it?", default=False, hint="inspect is read-only"):
            ui.info("Nothing was deleted.")
            ui.blank()
            return
    except NonInteractiveError as exc:
        ui.error(str(exc))
        return

    try:
        removed = bundle.delete_conversation(doomed.id)
    except BundleError as exc:
        ui.error(str(exc))
        ui.blank()
        return

    ui.success(
        f"Deleted {doomed.name} - {count_of(removed.files, 'file')}, "
        f"{_megabytes(removed.bytes_freed)} freed"
    )
    if removed.backup:
        ui.detail(f"copy kept at {removed.backup}")
    ui.blank()


def _delete_bundle(ui: UI, summary: BundleSummary) -> bool:
    """Remove a whole bundle. Returns ``True`` when it is gone."""
    try:
        ui.blank()
        ui.warn(f"Deleting the whole bundle: {summary.root}")
        ui.detail(
            f"{count_of(len(summary.conversations), 'conversation')}, "
            f"{_megabytes(summary.bytes_on_disk)}, made "
            f"{summary.manifest.created_at:%d %b %Y}"
        )
        ui.detail("Nothing puts this back. A bundle is the backup.")
        if not ui.confirm("Delete it?", default=False, hint="inspect is read-only"):
            ui.info("Nothing was deleted.")
            ui.blank()
            return False
    except NonInteractiveError as exc:
        ui.error(str(exc))
        return False

    try:
        removed = delete_bundle(summary.root)
    except (BundleError, OSError) as exc:
        ui.error(str(exc))
        ui.blank()
        return False

    ui.success(f"Deleted {summary.root.name} - {_megabytes(removed.bytes_freed)} freed")
    ui.blank()
    return True
