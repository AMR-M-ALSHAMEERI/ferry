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

import os
import shutil
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast
from uuid import UUID

from ferry.adapters.base import (
    Adapter,
    DetectResult,
    ExportEvent,
    ImportEvent,
    ImportOptions,
    OnConflict,
    RemoveEvent,
    RemoveOptions,
)
from ferry.adapters.census import count_of
from ferry.adapters.removal import Candidate, remove, survey
from ferry.cli.ui import UI, NonInteractiveError
from ferry.core import Bundle, BundleError, BundleSummary, delete_bundle, summarise
from ferry.core.bundle import MANIFEST_NAME
from ferry.core.compat import CEILING, assess, pair
from ferry.core.crypto import WrongPassphrase
from ferry.core.sealed import (
    SEALED_SUFFIX,
    is_sealed,
    opens_with,
    seal_bundle,
    unsealed,
)
from ferry.core.summary import ConversationSummary
from ferry.ucs import Conversation

__all__ = [
    "Scanned",
    "find_bundles",
    "run_compact",
    "run_export",
    "run_import",
    "run_inspect",
    "run_remove",
]

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


def _describe(path: Path) -> tuple[str, str]:
    """A bundle as a name and a sentence about it.

    Two parts rather than one line joined by a dash: the name is what someone
    is looking for and the rest is what tells them they have found it, and a
    dash between them makes the eye do work the layout should be doing. The
    name goes on the row; the sentence goes under it.

    A sealed bundle can only be described by its size and date -- everything
    else about it is encrypted, which is the point.
    """
    if path.is_file():
        if not is_sealed(path):
            return path.name, "Not a bundle."
        made = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        size = path.stat().st_size / 1024 / 1024
        return path.name, f"Sealed, {size:.1f} MB, made {made:%d %b %Y}."
    try:
        manifest = Bundle.open(path).manifest
    except BundleError:
        return path.name, "Cannot be read."
    tools = ", ".join(manifest.tools_included) or "nothing"
    plural = "" if manifest.conversation_count == 1 else "s"
    return path.name, (
        f"{manifest.conversation_count} conversation{plural} from {tools}, "
        f"made {manifest.created_at:%d %b %Y}."
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
    choices = [(str(p), *_describe(p)) for p in listed]
    choices.append((_TYPE_A_PATH, "Somewhere else", "Type the path yourself."))
    chosen = ui.select(question, choices, hint="use --bundle")
    if chosen is None:
        return None
    if chosen == _TYPE_A_PATH:
        return _typed_bundle(ui)
    return Path(chosen)


_QUOTES: Final = "\"'"


def clean_path(text: str) -> str:
    """What someone actually meant by what they pasted.

    Windows Explorer's "Copy as path" wraps the path in double quotes, and a
    path with quotes around it does not exist. Stripping them turns a
    mystifying "there is nothing there" into no error at all.
    """
    return text.strip().strip(_QUOTES).strip()


@dataclass(frozen=True)
class PathVerdict:
    """What a typed path turned out to be."""

    bundle: Path | None = None
    """Something Ferry can open -- a bundle directory, or a sealed file."""

    nearby: tuple[Path, ...] = ()
    """Bundles found *inside* the folder that was typed."""

    problem: str = ""
    """Why it is neither, phrased for the person who typed it."""


def bundle_at(text: str) -> PathVerdict:
    """Decide what a typed path is, without opening anything.

    Four answers rather than one, because "that did not work" is the least
    useful thing this screen can say. A path that does not exist, a folder
    that is not a bundle, a folder *holding* bundles, and a file that is not
    sealed are four different mistakes with four different next steps -- and
    the third one is not a mistake at all: pointing at the right neighbourhood
    deserves a list, not a complaint.
    """
    cleaned = clean_path(text)
    if not cleaned:
        return PathVerdict()
    path = Path(cleaned).expanduser()

    if path.is_file():
        if is_sealed(path):
            return PathVerdict(bundle=path)
        if path.suffix == SEALED_SUFFIX:
            # Named like one but does not begin like one. Saying only "not a
            # bundle" would leave someone staring at a file whose name says
            # otherwise.
            return PathVerdict(
                problem=f"{path.name} is named like a sealed bundle but does not begin like one."
            )
        return PathVerdict(problem=f"{path.name} is a file, not a bundle.")

    if not path.exists():
        return PathVerdict(problem=f"There is nothing at {path}")
    if not path.is_dir():
        return PathVerdict(problem=f"{path} is neither a folder nor a file Ferry can open.")
    if (path / MANIFEST_NAME).is_file():
        return PathVerdict(bundle=path)

    inside = tuple(find_bundles([path]))
    if inside:
        return PathVerdict(nearby=inside)
    return PathVerdict(problem=f"{path.name} is not a bundle - there is no {MANIFEST_NAME} in it.")


def _typed_bundle(ui: UI) -> Path | None:
    """Ask for a path, and keep asking until it names something real.

    A mistyped path used to end the screen: one stray character, one stale
    folder, one paste with a quote on the end, and you were back at the main
    menu with nothing to correct. Nothing is being guessed here -- unlike a
    passphrase, a path is checkable -- so there is no attempt limit. What you
    typed stays in the prompt as the starting text so a typo is a keystroke to
    fix rather than a line to type again, and escape still leaves on the first
    press.
    """
    ui.detail("Type or paste the folder holding the bundle. Tab completes it.")
    typed = ""
    while True:
        try:
            answer = ui.path("Path to the bundle", default=typed, hint="use --bundle")
        except NonInteractiveError as exc:
            ui.error(str(exc))
            return None
        if not answer or not clean_path(answer):
            return None
        typed = clean_path(answer)

        verdict = bundle_at(typed)
        if verdict.bundle is not None:
            return verdict.bundle
        if verdict.nearby:
            picked = _pick_nearby(ui, Path(typed).expanduser(), verdict.nearby)
            if picked is not None:
                return picked
            continue
        ui.error(verdict.problem)


def _pick_nearby(ui: UI, folder: Path, found: Sequence[Path]) -> Path | None:
    """That folder is not a bundle, but it holds some. Offer them.

    Typing the folder your bundles live in is close enough to right that
    refusing it would be pedantry.
    """
    ui.blank()
    ui.info(f"{folder.name} is not a bundle itself, but it holds {count_of(len(found), 'bundle')}.")
    listed = list(found[:_MAX_LISTED_BUNDLES])
    choices = [(str(path), *_describe(path)) for path in listed]
    choices.append((_TYPE_A_PATH, "None of these", "Type another path yourself."))
    try:
        chosen = ui.select("Which one?", choices, hint="use --bundle")
    except NonInteractiveError as exc:
        ui.error(str(exc))
        return None
    if chosen is None or chosen == _TYPE_A_PATH:
        return None
    return Path(chosen)


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


Event = ExportEvent | ImportEvent | RemoveEvent
"""Anything an adapter reports while it works. One screen renders all three."""


def _with(first: Event | None, rest: Iterator[Event]) -> Iterator[Event]:
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
    events: Iterator[Event],
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


def _actions(tool: str) -> list[tuple[str, str, str]]:
    """The import choices, naming the assistant being written into.

    A function rather than a constant because *"leaving anything already there
    alone"* was the old wording, and the person it was written for asked
    **"what is there?"** -- a fair question. "There" is the assistant, and the
    thing already there is a conversation it already has. Both are nameable, so
    they are named.
    """
    return [
        (
            "preview",
            "Preview it first",
            "Shows everything that would happen. Nothing is written.",
        ),
        (
            "choose",
            "Choose which conversations to import",
            "Tick the ones you want. Everything is ticked to begin with.",
        ),
        (
            "skip",
            f"Import, and keep the copy {tool} already has",
            "Any conversation it already has is left exactly as it is.",
        ),
        (
            "rename",
            "Import, and keep both copies",
            "The one from the bundle arrives beside it, under a new id.",
        ),
        (
            "overwrite",
            f"Import, and replace the copy {tool} already has",
            "The copy being replaced is saved to your Ferry backups folder first.",
        ),
        ("cancel", "Cancel", "Nothing is written."),
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
            ("here", str(here), "This machine's home folder."),
            ("other", "Somewhere else", "Choose a folder yourself."),
            ("leave", "Leave them as they are", "The paths inside stay as recorded."),
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


@dataclass(frozen=True)
class Opened:
    """A bundle directory ready to read, and how it came to be open."""

    root: Path
    """Where the bundle is. For a sealed one, a temporary copy."""

    passphrase: str | None = None
    """What it was unsealed with, or ``None`` if it was never sealed.

    Kept because sealing it again needs it, and asking a second time would be
    worse than useless: a bundle edited and then sealed under a *different*
    passphrase keeps its name and quietly stops opening the way it did
    yesterday. The same passphrase in, the same passphrase out.
    """


@contextmanager
def _opened(ui: UI, picked: Path) -> Iterator[Opened | None]:
    """Yield a directory holding the bundle, unsealing it first if it is sealed.

    Yields ``None`` when the user backed out of the passphrase or it was wrong,
    so every caller has one shape to handle. The unsealed copy is removed on
    the way out, including when the caller raises.
    """
    if not is_sealed(picked):
        yield Opened(root=picked)
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
                yield Opened(root=bundle.root, passphrase=passphrase)
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
        _import_from(ui, available, bundle_dir.root)


def _cross_tool_choices(tool: str, mixed: int) -> list[tuple[str, str, str]]:
    """What to do about conversations from another assistant.

    ``mixed`` is how many of the bundle's conversations came from ``tool``
    itself, and it is usually **zero**. An export writes one tool per bundle, so
    a bundle imported into a different assistant is normally foreign all the way
    through, and a bundle with both in it only happens when someone exports
    twice into the same folder.

    That is why the *skip* row is conditional. The first draft offered it
    always, so importing a Claude Code bundle into Antigravity put "import only
    the conversations that came from Antigravity" under the cursor -- an option
    that would have imported **nought of nineteen**. A default that does nothing
    is worse than a missing option, because someone pressing Enter to get past a
    screen they have already understood ends up with an empty result and no
    error to explain it.

    Nothing here writes: the import screen still follows, and *its* default is
    still Preview. This screen only decides what a conversion may give up.
    """
    rows = []
    if mixed:
        rows.append(
            (
                "skip",
                f"Import only the {mixed} that {tool} made, and skip the rest",
                "The others stay in the bundle. None of them is written.",
            )
        )
    rows += [
        (
            "archive",
            "Convert them so I can read and search them here",
            "Keeps the most detail. You can read them, but not continue them.",
        ),
        (
            "continue",
            "Convert them so I can carry on working in them",
            "Drops the assistant's thinking and tool output so you can continue.",
        ),
        ("cancel", "Cancel", "Nothing is written."),
    ]
    return rows


def _cross_tool(ui: UI, bundle: Bundle, adapter: Adapter) -> str | None:
    """What to do about foreign conversations, having said what it costs.

    Returns ``"skip"``, ``"archive"``, ``"continue"``, or ``None`` to cancel.
    Says nothing at all when everything in the bundle already belongs to the
    target, which is the ordinary restore -- that path should not be
    interrupted to explain a feature it is not using.
    """
    native: list[Conversation] = []
    foreign: list[Conversation] = []
    for found in bundle.list_conversations():
        try:
            item = bundle.load_conversation(found)
        except Exception:  # noqa: BLE001 - a bad file is the importer's to report
            continue
        (native if item.source_tool == adapter.name else foreign).append(item)
    if not foreign:
        return "skip"

    impossible = [c for c in foreign if pair(c.source_tool, adapter.name).support == "unsupported"]
    convertible = [c for c in foreign if c not in impossible]

    ui.blank()
    sources = ", ".join(sorted({c.source_tool for c in foreign}))
    if native:
        ui.info(
            f"This bundle holds {len(foreign)} conversations from {sources} "
            f"and {len(native)} from {adapter.display_name}."
        )
    else:
        # The ordinary case, said plainly. "19 of 19 came from elsewhere" is
        # arithmetic standing where a sentence belongs.
        ui.info(
            f"This bundle holds {len(foreign)} conversations from {sources}. "
            f"Writing them into {adapter.display_name} means converting them."
        )

    if impossible:
        reason = pair(impossible[0].source_tool, adapter.name).reason
        ui.warn(f"{len(impossible)} of them cannot be written into {adapter.display_name}.")
        ui.info(reason)
        if not convertible:
            ui.info("There is nothing here Ferry can import.")
            ui.blank()
            return None

    ui.blank()
    ui.info("Converting costs:")
    for note in _combined_loss(convertible, adapter.name):
        ui.info(f"  {note}")
    ui.blank()

    choice = ui.select(
        f"How should Ferry bring these into {adapter.display_name}?",
        _cross_tool_choices(adapter.display_name, len(native)),
        hint="use --allow-cross-tool",
    )
    if choice is None or choice == "cancel":
        return None
    return choice


def _combined_loss(items: list[Conversation], target: str) -> list[str]:
    """One summary of what a whole batch loses, with the counts added up.

    Per-conversation notes would be nineteen paragraphs saying the same four
    things with different numbers, and a wall of text is read as carefully as
    no text at all. The counts are summed because that is the number a person is
    actually deciding about.
    """
    signatures = calls = images = 0
    for item in items:
        loss = assess(item, target)
        for note in loss.notes:
            number = int(note.split(" ", 1)[0]) if note[:1].isdigit() else 0
            if "signature" in note:
                signatures += number
            elif "tool calls" in note:
                calls += number
            elif "image" in note:
                images += number

    summary: list[str] = []
    if signatures:
        summary.append(
            f"{signatures} thinking blocks lose their signature - it is issued by "
            f"the model's vendor and cannot be reissued outside it"
        )
    if calls:
        summary.append(f"{calls} tool calls and results become readable text, not runnable calls")
    if images:
        summary.append(f"{images} image blocks need their bytes in the bundle to survive")
    summary.append("each conversation stays attributed to the tool and model that produced it")
    summary.append(CEILING)
    summary.append(
        "your backup is not changed by any of this - the bundle keeps everything, "
        "and importing it again gives it all back"
    )
    return summary


def _choose_conversations(ui: UI, bundle: Bundle, only: frozenset[str]) -> frozenset[str] | None:
    """Tick which conversations to import. ``None`` if the person backed out.

    Offered as a row on the action screen rather than as a step of its own.
    Importing a whole bundle is what almost everyone wants almost every time,
    and a checklist standing in front of that would be a screen answered
    identically by nearly everyone who met it.

    Everything starts ticked, so the person who opened this out of curiosity
    leaves it in the state they found it.
    """
    rows: list[tuple[str, str]] = []
    for found in bundle.list_conversations():
        try:
            item = bundle.load_conversation(found)
        except Exception:  # noqa: BLE001 - the importer reports a bad file
            rows.append((str(found), f"{found}  (cannot be read)"))
            continue
        title = _clip(item.title or "untitled", _MAX_TITLE)
        rows.append((str(found), f"{title}  ({item.source_tool}, {len(item.messages)} messages)"))

    if not rows:
        return only

    picked = ui.multiselect(
        "Which conversations should be imported?",
        rows,
        preselected=sorted(only) if only else [value for value, _ in rows],
        hint="use --conversation",
    )
    if picked is None:
        return None
    return frozenset(picked)


def _trust_folders(
    ui: UI, adapter: Adapter, bundle_dir: Path, options: ImportOptions
) -> bool | None:
    """Offer to let the target open the folders it is about to be written into.

    Returns whether to grant it, or ``None`` to cancel the import. Says nothing
    when there is nothing to grant, which is every ordinary restore into
    folders already in use.

    Asked **after** the write has been agreed to, not before. Before, it is a
    question about a thing that may not happen; after, it is the last step of
    something already decided, and the answer applies to exactly the
    conversations chosen rather than to the whole bundle.
    """
    folders = adapter.unopenable(bundle_dir, options)
    if not folders:
        return False

    ui.blank()
    many = len(folders) > 1
    ui.info(
        f"{adapter.display_name} will not open a conversation in a folder it has not "
        f"been told about, and {'these are' if many else 'this one is'} new to it:"
    )
    for folder in folders:
        ui.info(f"  {folder}")

    choice = ui.select(
        "May Ferry add them to the list it can open?",
        [
            (
                "grant",
                f"Yes, let {adapter.display_name} open them",
                "The same thing you would agree to by starting it in each folder yourself. "
                "Ferry saves the current settings first.",
            ),
            (
                "leave",
                "No, I will do it myself",
                "The conversations are still written. Ferry names the folders and you start "
                f"{adapter.display_name} once in each.",
            ),
        ],
    )
    if choice is None:
        return None
    return choice == "grant"


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

        # Asked before the write screen, not after it: what a person is
        # agreeing to on the next screen depends on the answer to this one.
        mode = _cross_tool(ui, bundle, adapter)
        if mode is None:
            ui.info("Nothing was written.")
            ui.blank()
            return

        # The only screen in Ferry that writes into a user's real conversation
        # history. It says so, and the option under the cursor is the one that
        # writes nothing.
        only: frozenset[str] = frozenset()
        while True:
            ui.blank()
            ui.warn(f"This writes into your real {adapter.display_name} history.")
            showing = len(only) if only else count
            action = ui.select(
                f"Import {showing} conversations?",
                _actions(adapter.display_name),
                hint="use --dry-run / --on-conflict",
            )
            if action != "choose":
                break
            # Back to this screen afterwards, with the count updated. Choosing
            # what to import and choosing what to do about a conflict are two
            # questions, and answering the first should not commit the second.
            picked = _choose_conversations(ui, bundle, only)
            if picked is not None:
                only = frozenset() if len(picked) == count else picked
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
            adapter.import_(
                bundle_dir,
                ImportOptions(
                    dry_run=True,
                    path_remap=remap,
                    allow_cross_tool=mode != "skip",
                    mode="continue" if mode == "continue" else "archive",
                    only=only,
                ),
            ),
            "conversations would be imported",
            label="Previewing",
            total=len(only) if only else count,
        )
        try:
            action = ui.select(
                "Import for real?",
                # Neither "preview" nor "choose" again: the preview just ran, and
                # what to import was settled before it.
                [
                    choice
                    for choice in _actions(adapter.display_name)
                    if choice[0] not in ("preview", "choose")
                ],
                hint="use --on-conflict",
            )
        except NonInteractiveError as exc:
            ui.error(str(exc))
            return
        if action is None or action == "cancel":
            ui.info("Nothing was written.")
            ui.blank()
            return

    settled = ImportOptions(
        on_conflict=_conflict(action),
        path_remap=remap,
        allow_cross_tool=mode != "skip",
        mode="continue" if mode == "continue" else "archive",
        only=only,
    )
    try:
        trust = _trust_folders(ui, adapter, bundle_dir, settled)
    except NonInteractiveError as exc:
        ui.error(str(exc))
        return
    if trust is None:
        ui.info("Nothing was written.")
        ui.blank()
        return

    ui.blank()
    _report(
        ui,
        adapter.import_(bundle_dir, replace(settled, trust_folders=trust)),
        "conversations imported",
        label="Importing",
        total=len(only) if only else count,
    )


# --------------------------------------------------------------------------
# deleting what Ferry imported
# --------------------------------------------------------------------------

_DELETE_HINT = "deleting asks before it removes anything, so it needs a terminal"


def _removal_line(candidate: Candidate) -> str:
    """One conversation Ferry imported, as a row someone can choose from."""
    parts: list[str] = []
    if candidate.came_from:
        parts.append(f"from {candidate.came_from}")
    if candidate.imported_at is not None:
        parts.append(f"imported {candidate.imported_at:%d %b %Y}")
    name = _clip(candidate.name, _MAX_TITLE)
    return f"{name:<{_MAX_TITLE}}  {', '.join(parts)}".rstrip()


def _tally(found: Sequence[Candidate]) -> str:
    can = sum(1 for candidate in found if candidate.removable)
    stay = len(found) - can
    return f"{can} can be deleted" + (f", {stay} will stay." if stay else ".")


def run_remove(ui: UI, adapters: Scanned) -> None:
    """Delete conversations Ferry imported, from the assistant it put them in.

    Only ever what Ferry wrote, and only while it is still exactly what Ferry
    wrote: a conversation someone has opened and carried on is theirs, and is
    listed as staying rather than offered.

    **Nothing is ticked to begin with**, the opposite of the import checklist.
    Taking everything is the ordinary answer to "which should be imported?";
    here the ordinary answer is one or two, and a list that opened fully ticked
    would make the destructive answer the one Enter gives.
    """
    found_in: list[tuple[Adapter, list[Candidate]]] = []
    for adapter in _installed(adapters):
        found = survey(adapter)
        if found:
            found_in.append((adapter, found))

    if not found_in:
        ui.info("There is nothing here Ferry imported from another assistant.")
        ui.detail(
            "Only conversations Ferry converted from another assistant can be deleted "
            "here. A restore puts back your own conversation, and that is not Ferry's "
            "to delete."
        )
        ui.blank()
        return

    try:
        if len(found_in) == 1:
            adapter, found = found_in[0]
            ui.info(f"{adapter.display_name} is the only assistant Ferry has imported into.")
        else:
            chosen = ui.select(
                "Delete from which assistant?",
                [(a.name, a.display_name, _tally(listed)) for a, listed in found_in],
                hint=_DELETE_HINT,
            )
            if chosen is None:
                return
            adapter, found = next(pair for pair in found_in if pair[0].name == chosen)

        removable = [candidate for candidate in found if candidate.removable]
        staying = [candidate for candidate in found if not candidate.removable]
        if staying:
            ui.blank()
            ui.info(f"{count_of(len(staying), 'conversation')} Ferry imported will stay:")
            for candidate in staying[:_MAX_SHOWN_WARNINGS]:
                ui.detail(f"{_clip(candidate.name, _MAX_TITLE)}: {candidate.why_it_stays}")
            if len(staying) > _MAX_SHOWN_WARNINGS:
                ui.detail(f"... and {len(staying) - _MAX_SHOWN_WARNINGS} more")
        if not removable:
            ui.info("Nothing here can be deleted.")
            ui.blank()
            return

        # Said before the list rather than after the choice. Ticking three
        # conversations and only then being told to close the app is a screen
        # wasting the person's time.
        busy = adapter.in_use()
        if busy:
            ui.blank()
            ui.warn(busy)
            ui.info("Nothing was deleted.")
            ui.blank()
            return

        ui.blank()
        picked = ui.multiselect(
            "Which should be deleted?",
            [(str(candidate.record_id), _removal_line(candidate)) for candidate in removable],
            preselected=[],
            hint=_DELETE_HINT,
        )
        doomed = [c for c in removable if picked is not None and str(c.record_id) in picked]
        if not doomed:
            ui.info("Nothing was deleted.")
            ui.blank()
            return

        listed = doomed[0].path is not None and adapter.listing(doomed[0].path) is not None
        ui.blank()
        ui.warn(
            f"This deletes {count_of(len(doomed), 'conversation')} from your real "
            f"{adapter.display_name} history."
        )
        ui.detail("Each is exactly as Ferry wrote it. None has been carried on since.")
        if listed:
            ui.detail(f"Its entry in {adapter.display_name}'s list goes with it.")
        ui.info("A copy of each goes to ~/.ferry/backups first.")
        question = "Delete it?" if len(doomed) == 1 else "Delete them?"
        if not ui.confirm(question, default=False, hint=_DELETE_HINT):
            ui.info("Nothing was deleted.")
            ui.blank()
            return
    except NonInteractiveError as exc:
        ui.error(str(exc))
        return

    ui.blank()
    _report(
        ui,
        remove(adapter, RemoveOptions(only=frozenset(str(c.record_id) for c in doomed))),
        "conversations deleted",
        label="Deleting",
        total=len(doomed),
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
        return f"{name}  (cannot be read: {_clip(summary.unreadable, 40)})"
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

    with _opened(ui, picked) as opened:
        if opened is None:
            return
        if opened.passphrase is None:
            _inspect_at(ui, opened.root)
            return
        _inspect_sealed(ui, picked, opened)


def _inspect_at(ui: UI, picked: Path) -> None:
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


def _inspect_sealed(ui: UI, archive: Path, opened: Opened) -> None:
    """The inspect screen for a sealed bundle.

    This screen used to end at "To change a sealed bundle, unseal it first" --
    an instruction Ferry offered no way to follow. Two ways out of that, and
    both are here because they suit different days.

    **Unsealing to a folder** is the plain one: it writes the bundle out where
    you can work with it, leaves the sealed file exactly as it was, and hands
    you back the ordinary screen. Nothing is at risk because nothing existing
    is rewritten.

    **Deleting and sealing again** is the convenient one, and it is the same
    act underneath -- unseal, edit, seal, prove the new file opens, and only
    then let it replace the old one. Convenience that skipped the proving step
    would be a backup tool destroying a backup.
    """
    assert opened.passphrase is not None
    while True:
        try:
            bundle = Bundle.open(opened.root)
        except BundleError as exc:
            ui.error(str(exc))
            ui.blank()
            return

        with ui.scanning(f"Reading {archive.name}"):
            summary = summarise(bundle)
        _show(ui, summary)
        ui.detail("Sealed. Nothing in the file changes until you choose one of these.")

        choices = [("done", "Done")]
        if summary.conversations:
            choices.append(("one", "Delete one conversation, then seal it again"))
        choices.append(("unseal", "Unseal it into a folder I can work with"))
        choices.append(("all", "Delete this sealed bundle"))
        try:
            action = ui.select("Anything else?", choices, hint="inspect is read-only")
        except NonInteractiveError as exc:
            ui.error(str(exc))
            return

        if action is None or action == "done":
            return
        if action == "unseal":
            _unseal_to_folder(ui, archive, opened.root)
            continue
        if action == "all":
            if _delete_sealed(ui, archive, summary):
                return
            continue
        if _delete_conversation(ui, bundle, summary, sealed=True):
            if not _reseal(ui, archive, opened.root, opened.passphrase):
                return


def _unseal_to_folder(ui: UI, archive: Path, source: Path) -> Path | None:
    """Write the opened bundle somewhere that outlives this screen.

    The copy being read already exists; this only puts it somewhere permanent.
    The sealed file is not touched, so there is a moment where both exist --
    which is the safe order, and the reason this is the option to reach for
    first.
    """
    ui.blank()
    ui.warn("An unsealed bundle is not encrypted.")
    ui.info("It is every conversation in it, readable by anything on this machine.")
    ui.detail("The sealed file stays where it is. Delete the folder when you are done.")

    default = str(archive.parent / archive.with_suffix("").name)
    while True:
        try:
            answer = ui.path("Unseal it to", default=default, hint="use --into")
        except NonInteractiveError as exc:
            ui.error(str(exc))
            return None
        if not answer or not clean_path(answer):
            ui.info("Nothing was unsealed.")
            ui.blank()
            return None
        destination = Path(clean_path(answer)).expanduser()
        if destination.exists():
            ui.error(f"{destination} is already there. Ferry will not write over it.")
            default = str(destination)
            continue
        break

    try:
        with ui.scanning(f"Unsealing to {destination.name}"):
            shutil.copytree(source, destination)
    except OSError as exc:
        ui.error(f"could not unseal to {destination}: {exc}")
        ui.blank()
        return None

    ui.success(f"Unsealed: {destination}")
    ui.detail("Open it from Inspect to delete anything from it, then seal it again from Export.")
    ui.blank()
    return destination


def _reseal(ui: UI, archive: Path, root: Path, passphrase: str) -> bool:
    """Write the edited bundle back over the sealed file. Returns success.

    **Never in place.** The new file is written beside the old one, opened
    again with the same passphrase to prove it is readable, and only then does
    it replace the original -- one rename, which the filesystem does whole or
    not at all. A crash at any point before that leaves yesterday's file
    untouched, which is the property that makes editing a sealed bundle
    something a backup tool may reasonably offer.
    """
    staged = archive.with_name(archive.name + ".new")
    try:
        with ui.scanning("Sealing it again"):
            sealed = seal_bundle(root, passphrase, staged)
    except (BundleError, OSError) as exc:
        ui.error(f"could not seal the bundle: {exc}")
        _discard(staged)
        ui.detail(f"{archive.name} is unchanged.")
        ui.blank()
        return False

    with ui.scanning("Checking it opens"):
        confirmed = opens_with(sealed.path, passphrase)
    if not confirmed:
        # Should be impossible, and is checked anyway: this is the one place
        # where being wrong costs the user everything.
        ui.error("The new file did not open with that passphrase.")
        ui.detail(f"{archive.name} is unchanged, and the new file was discarded.")
        _discard(staged)
        ui.blank()
        return False

    try:
        os.replace(staged, archive)
    except OSError as exc:
        ui.error(f"could not replace {archive.name}: {exc}")
        ui.detail(f"The edited copy is at {staged}, and the original is untouched.")
        ui.blank()
        return False

    ui.success(
        f"{archive.name} sealed again - {count_of(sealed.conversations, 'conversation')}, "
        f"{_megabytes(sealed.bytes_written)}"
    )
    ui.blank()
    return True


def _discard(staged: Path) -> None:
    """Remove a half-made sealed file. Failing to is not worth an error."""
    try:
        staged.unlink(missing_ok=True)
    except OSError:
        pass


def _delete_sealed(ui: UI, archive: Path, summary: BundleSummary) -> bool:
    """Remove a sealed bundle -- the one file. Returns ``True`` when it is gone."""
    try:
        size = archive.stat().st_size
    except OSError as exc:
        ui.error(f"could not read {archive}: {exc}")
        return False
    try:
        ui.blank()
        ui.warn(f"Deleting the sealed bundle: {archive}")
        ui.detail(
            f"{count_of(len(summary.conversations), 'conversation')}, "
            f"{_megabytes(size)}, made {summary.manifest.created_at:%d %b %Y}"
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
        archive.unlink()
    except OSError as exc:
        ui.error(f"could not delete {archive}: {exc}")
        ui.blank()
        return False

    ui.success(f"Deleted {archive.name} - {_megabytes(size)} freed")
    ui.blank()
    return True


def _delete_conversation(
    ui: UI, bundle: Bundle, summary: BundleSummary, *, sealed: bool = False
) -> bool:
    """Pick one conversation and remove it, its attachments and its original file.

    Returns ``True`` when something was actually deleted, which is what tells
    a sealed bundle it needs sealing again.
    """
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
            return False
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
        if sealed:
            # Said out loud, because it is the one thing about this that is
            # not obvious: the safety copy is a plain folder, even though the
            # bundle it came out of is encrypted.
            ui.detail("That copy is not encrypted, even though this bundle is.")
        if not ui.confirm("Delete it?", default=False, hint="inspect is read-only"):
            ui.info("Nothing was deleted.")
            ui.blank()
            return False
    except NonInteractiveError as exc:
        ui.error(str(exc))
        return False

    try:
        removed = bundle.delete_conversation(doomed.id)
    except BundleError as exc:
        ui.error(str(exc))
        ui.blank()
        return False

    ui.success(
        f"Deleted {doomed.name} - {count_of(removed.files, 'file')}, "
        f"{_megabytes(removed.bytes_freed)} freed"
    )
    if removed.backup:
        ui.detail(f"copy kept at {removed.backup}")
    ui.blank()
    return True


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


# --------------------------------------------------------------------------
# compact
# --------------------------------------------------------------------------

_SHAPE_CHOICES: Final = [
    ("handoff", "A handoff, to resume this in a new session", "Everything below, together."),
    ("said", "Just what I said", "Your own messages, quoted."),
    ("done", "What was done", "Files touched, commands run, and errors seen."),
]

_LENGTH_CHOICES: Final = [
    ("standard", "Standard", "About a page."),
    ("brief", "Brief", "The shortest useful version."),
    ("full", "Full", "Everything that survives the cut."),
]


def run_compact(ui: UI) -> None:
    """Turn one conversation into a document worth pasting somewhere else.

    Reads a bundle, never a live store. A bundle is a snapshot; a conversation
    still being written is a moving target, and compacting one would give a
    different answer every time it was asked. Reaching into a live store would
    also mean a second way into people's real data for no gain -- Ferry reads a
    store in exactly two places, `detect` and `export`, and that stays true.

    Everything here happens on this machine. There is no key to configure, no
    account, no network call, and no way for a conversation to leave.
    """
    while True:
        try:
            picked = _choose_bundle(ui, "Which bundle holds the conversation?")
        except NonInteractiveError as exc:
            ui.error(str(exc))
            return
        if picked is None:
            return

        with _opened(ui, picked) as opened:
            if opened is None:
                # A wrong passphrase, or escape at the prompt. Back to the
                # bundle list rather than out to the main menu: wanting a
                # different bundle is the likeliest reason to be here.
                continue
            if not _compact_at(ui, opened.root):
                return


def _compact_at(ui: UI, root: Path) -> bool:
    """The compact screen, on an opened bundle.

    Four questions in a row -- which conversation, what for, how long, and then
    what to do with it -- and **escape goes back exactly one of them.**

    It used to mean three different things across three consecutive prompts:
    leave the screen entirely, go back one, and go back two. That is worse than
    a key that does nothing, because someone who presses it once and loses
    their place stops trusting it everywhere. The cursor is restored to the row
    they were on, so going back to change one answer costs one keystroke rather
    than re-navigating a list of forty conversations.

    Returns:
        ``True`` when the user asked to go back to the bundle list, ``False``
        when they are finished.
    """
    try:
        bundle = Bundle.open(root)
    except BundleError as exc:
        ui.error(str(exc))
        ui.blank()
        return False

    with ui.scanning(f"Reading {root.name}"):
        summary = summarise(bundle)

    if not summary.conversations:
        ui.info("There is nothing in this bundle to compact.")
        ui.blank()
        return False

    rows = [
        (str(conversation.id), _conversation_line(conversation))
        for conversation in summary.conversations
    ]
    # Where the cursor sat, per question, so going back lands where you left.
    at = {"conversation": 0, "shape": 0, "length": 0}
    step = "conversation"
    chosen = shape = length = ""

    while True:
        try:
            if step == "conversation":
                answer = ui.select(
                    "Which conversation?",
                    rows,
                    hint="use --conversation",
                    initial=at["conversation"],
                    back=True,
                )
                if answer is None:
                    return True
                chosen, at["conversation"] = answer, _index_of(rows, answer)
                step = "shape"
                continue

            if step == "shape":
                answer = ui.select(
                    "What should it be for?",
                    _SHAPE_CHOICES,
                    hint="use --shape",
                    initial=at["shape"],
                    back=True,
                )
                if answer is None:
                    step = "conversation"
                    continue
                shape, at["shape"] = answer, _index_of(_SHAPE_CHOICES, answer)
                step = "length"
                continue

            if step == "length":
                answer = ui.select(
                    "How long?",
                    _LENGTH_CHOICES,
                    hint="use --length",
                    initial=at["length"],
                    back=True,
                )
                if answer is None:
                    step = "shape"
                    continue
                length, at["length"] = answer, _index_of(_LENGTH_CHOICES, answer)
                step = "show"
                continue
        except NonInteractiveError as exc:
            ui.error(str(exc))
            return False

        conversation_id = next(c.id for c in summary.conversations if str(c.id) == chosen)
        try:
            with ui.scanning("Compacting"):
                document = _compact_document(bundle, conversation_id, shape=shape, length=length)
        except BundleError as exc:
            ui.error(str(exc))
            ui.blank()
            return False

        _show_compact(ui, document)
        outcome = _after_compact(ui, document, summary.root.name)
        if outcome == "done":
            return False
        if outcome == "bundle":
            return True
        # "another" and "again" both land back in the list of questions, the
        # first at the top and the second on the settings just used.
        step = "conversation" if outcome == "another" else "length"


def _index_of(rows: Sequence[tuple[str, ...]], value: str) -> int:
    """Where a value sits in a list of choices, for restoring the cursor."""
    return next((n for n, row in enumerate(rows) if row[0] == value), 0)


def _compact_document(bundle: Bundle, conversation_id: UUID, *, shape: str, length: str) -> str:
    """The document itself. Imported here so the menu does not pay for the
    compact package on every run of a screen that never opens it."""
    from ferry import __version__
    from ferry.compact import compact

    conversation = bundle.load_conversation(conversation_id)
    return compact(conversation, shape=shape, length=length, version=__version__)


def _show_compact(ui: UI, document: str) -> None:
    """Put the document on the screen, and say plainly where it came from.

    The honesty line is shown *before* the document rather than buried under
    it, because it is the thing a person needs in order to know how to read
    what follows.
    """
    ui.blank()
    ui.info("Built from your conversation without sending it anywhere.")
    ui.detail("Nothing was invented - every line is quoted from it or counted from it.")
    ui.blank()
    for line in document.splitlines():
        ui.detail(line) if line.startswith(("#", "-", ">", "*")) else ui.info(line)
    ui.blank()


def _after_compact(ui: UI, document: str, bundle_name: str) -> str:
    """Copy it, save it, or go somewhere.

    Returns:
        ``done``, ``another`` (a different conversation), ``again`` (the same
        one at a different shape or length), or ``bundle`` (a different
        bundle). The caller owns the steps; this only says where to go.
    """
    from ferry.cli import clipboard

    while True:
        # Done first, the same way every other screen in Ferry orders this
        # question: the highlighted row when the menu opens should be the one
        # that changes nothing.
        choices: list[tuple[str, str]] = [("done", "Done")]
        if clipboard.available():
            # Offered only where there is something to copy to. On a server or
            # over SSH there is not, and an option that fails when chosen is
            # worse than one that was never there.
            choices.append(("copy", "Copy it to the clipboard"))
        choices.append(("save", "Save it to a file"))
        # Named separately because they are different intentions. Wanting the
        # same conversation shorter is not wanting a different conversation,
        # and making someone walk back through the whole list to say so is the
        # dead end this screen had.
        choices.append(("again", "Try a different shape or length"))
        choices.append(("another", "Compact a different conversation"))
        choices.append(("bundle", "Open a different bundle"))

        try:
            action = ui.select("What now?", choices, hint="use --out")
        except NonInteractiveError as exc:
            ui.error(str(exc))
            return "done"

        if action is None or action == "done":
            return "done"
        if action in ("again", "another", "bundle"):
            return action
        if action == "copy":
            if clipboard.copy(document):
                ui.success("Copied.")
            else:
                # Not an error. The document is still on the screen, and saving
                # it is right there.
                ui.info("The clipboard did not take it. You can save it to a file instead.")
            ui.blank()
            continue

        _save_compact(ui, document, bundle_name)


def _save_compact(ui: UI, document: str, bundle_name: str) -> None:
    """Write the document where the person asks, without overwriting anything."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    default = str(Path.cwd() / f"compact-{bundle_name}-{stamp}.md")
    while True:
        try:
            answer = ui.path("Save it to", default=default, hint="use --out")
        except NonInteractiveError as exc:
            ui.error(str(exc))
            return
        if answer is None:
            return

        target = Path(clean_path(answer)).expanduser()
        if target.exists():
            ui.error(f"There is already something at {target}.")
            ui.detail("Choose another name - Ferry does not write over a file that exists.")
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(document, encoding="utf-8", newline="\n")
        except OSError as exc:
            ui.error(f"Could not write it: {exc}")
            continue
        ui.success(f"Saved to {target}")
        ui.blank()
        return
