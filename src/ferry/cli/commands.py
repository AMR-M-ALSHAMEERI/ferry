"""``ferry export`` and ``ferry import``: the menu's work, without the menu.

For scripts, for an assistant driving Ferry from a shell (PLAN.md §5 M8), and
for anyone who already knows what they want. Every question the screens ask is
a flag here, and a question with no flag and no terminal to ask it on is an
error that names the flag -- never a prompt that hangs waiting for input nobody
can give.

Nothing is decided differently. The same adapters run with the same options,
the same events are rendered by the same code, and the safe answer is still the
default: an import keeps a conversation the tool already has, backs up before
it writes, never converts across tools unless told to, and never edits another
tool's settings to trust a folder -- only the screen offers that, because it
is a security decision someone should see being made.
"""

from __future__ import annotations

import shutil
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Final

from ferry.adapters import REGISTRY
from ferry.adapters.base import Adapter, ConversionMode, DetectResult, ImportOptions, OnConflict
from ferry.adapters.census import count_of
from ferry.cli.flows import (
    PASSPHRASE_TRIES,
    Event,
    _ask_new_passphrase,
    _combined_loss,
    _default_bundle_name,
    _opened,
    _passphrase_refused,
    _recorded_home,
    _report,
    bundle_at,
    seal_checked,
)
from ferry.cli.ui import UI, NonInteractiveError
from ferry.core import Bundle, BundleError
from ferry.core.compat import pair
from ferry.core.crypto import WrongPassphrase
from ferry.core.sealed import SEALED_SUFFIX, is_sealed, unsealed
from ferry.ucs import Conversation

__all__ = ["FAILED", "OK", "PASSPHRASE_ENV", "REFUSED", "export_bundle", "import_bundle"]

OK: Final = 0

FAILED: Final = 1
"""The work ran and part of it failed, or the tool was not there to work on."""

REFUSED: Final = 2
"""The command could not start -- a flag was wrong or missing. Nothing was written."""

PASSPHRASE_ENV: Final = "FERRY_PASSPHRASE"
"""Where a script can put a passphrase instead of on the command line.

A flag's value lands in shell history and, while Ferry runs, in the process
list any other user on the machine can read. An environment variable does
neither, so it is the one the help recommends.
"""

_MAX_LISTED: Final = 10


def _tallied(events: Iterator[Event], kinds: Counter[str]) -> Iterator[Event]:
    """Pass events through, counting them, so the exit code can say what happened."""
    for event in events:
        kinds[event.kind] += 1
        yield event


def _found(ui: UI, tool: str) -> tuple[Adapter, DetectResult] | None:
    """The named assistant, if it is on this machine; says why not otherwise."""
    adapter = REGISTRY[tool]
    try:
        result = adapter.detect()
    except Exception as exc:  # noqa: BLE001 - contract says never raise; trust nothing
        why = f"detection failed: {exc.__class__.__name__}"
        result = DetectResult(installed=False, notes=[why])
    if result.installed:
        return adapter, result
    ui.error(f"{adapter.display_name} was not found on this machine.")
    for note in result.notes:
        ui.info(note)
    ui.blank()
    return None


def _new_passphrase(ui: UI) -> str | None:
    """A passphrase to seal with, asked twice -- or an error naming the flag."""
    if not ui.interactive:
        ui.error("Sealing needs a passphrase, and there is no terminal to ask for one.")
        ui.info(f"Set {PASSPHRASE_ENV}, or pass --passphrase.")
        return None
    try:
        return _ask_new_passphrase(ui)
    except NonInteractiveError as exc:
        ui.error(str(exc))
        return None


def export_bundle(
    ui: UI,
    *,
    tool: str,
    output: str = "",
    force: bool = False,
    encrypt: bool = False,
    passphrase: str | None = None,
    replace: bool = False,
) -> int:
    """Export every conversation ``tool`` holds into a bundle. Returns an exit code.

    Everything that can refuse does so **before** the export runs. An export
    can take minutes, and finding out afterwards that there was no passphrase
    to seal it with wastes them -- or worse, leaves someone who asked for an
    encrypted bundle holding an unencrypted one.
    """
    if replace and not encrypt:
        ui.error("--replace deletes the unencrypted bundle once it is sealed.")
        ui.info("It only means something with --encrypt.")
        return REFUSED

    target = Path(output).expanduser() if output else Path.cwd() / _default_bundle_name()
    if target.exists() and not target.is_dir():
        ui.error(f"{target} is a file. --output names the folder the bundle is written into.")
        return REFUSED
    if target.is_dir() and any(target.iterdir()) and not force:
        ui.error(f"{target} already has something in it.")
        ui.info("Pass --force to add to it. That is also how an interrupted export carries on.")
        return REFUSED

    secret = passphrase or None
    if encrypt:
        sealed_at = target.with_name(target.name + SEALED_SUFFIX)
        if sealed_at.exists() and not force:
            ui.error(f"There is already a sealed bundle at {sealed_at}.")
            ui.info("Pass --force to replace it.")
            return REFUSED
        if secret is None:
            secret = _new_passphrase(ui)
            if secret is None:
                return REFUSED

    found = _found(ui, tool)
    if found is None:
        return FAILED
    adapter, detected = found

    kinds: Counter[str] = Counter()
    ui.blank()
    _report(
        ui,
        _tallied(adapter.export(target), kinds),
        "conversations exported",
        label="Exporting",
        total=detected.conversation_count_estimate,
    )
    ui.info(f"Bundle: {target}")
    ui.blank()

    if encrypt and secret is not None:
        if seal_checked(ui, target, secret) is None:
            return FAILED
        if replace:
            try:
                shutil.rmtree(target)
            except OSError as exc:
                ui.error(f"could not remove {target}: {exc}")
                return FAILED
            ui.info(f"Removed {target.name}. The sealed file is now the only copy.")
        else:
            ui.detail(f"The unencrypted bundle is still at {target}")
        ui.blank()

    return FAILED if kinds["error"] else OK


@contextmanager
def _readable(ui: UI, picked: Path, passphrase: str | None) -> Iterator[Path | None]:
    """The bundle as a folder, unsealing it first if needed. ``None`` if it cannot be.

    A passphrase that was given is tried **once**. Retrying the same string
    cannot change the answer, and a script has nobody to type a second one.
    With none given, a terminal gets the screen's own prompt and its three
    tries; no terminal gets an error naming the flag.
    """
    if not is_sealed(picked):
        yield picked
        return

    if passphrase is None:
        if not ui.interactive:
            ui.error(f"{picked.name} is sealed, and there is no terminal to ask on.")
            ui.info(f"Set {PASSPHRASE_ENV}, or pass --passphrase.")
            yield None
            return
        with _opened(ui, picked) as opened:
            yield None if opened is None else opened.root
        return

    # Opened outside the `yield` rather than around it: an exception raised
    # by the caller's own work is thrown back in at the `yield`, and a handler
    # for OSError wrapped around it would catch that and report it as a
    # bundle that failed to open.
    stack = ExitStack()
    try:
        with ui.scanning("Opening"):
            bundle = stack.enter_context(unsealed(picked, passphrase))
    except WrongPassphrase:
        stack.close()
        _passphrase_refused(ui, PASSPHRASE_TRIES)
        yield None
        return
    except (BundleError, OSError) as exc:
        stack.close()
        ui.error(str(exc))
        yield None
        return
    with stack:
        yield bundle.root


def _parse_remap(rules: Sequence[str]) -> tuple[tuple[str, str], ...] | None:
    """``OLD=NEW`` rules as the pairs an import takes, or ``None`` if one is malformed."""
    parsed: list[tuple[str, str]] = []
    for rule in rules:
        old, sep, new = rule.partition("=")
        if not sep or not old.strip() or not new.strip():
            return None
        parsed.append((old.strip(), str(Path(new.strip()).expanduser())))
    return tuple(parsed)


def _split(
    bundle: Bundle, tool: str, only: frozenset[str]
) -> tuple[list[Conversation], list[Conversation]]:
    """The chosen conversations, as those that came from ``tool`` and those that did not."""
    native: list[Conversation] = []
    foreign: list[Conversation] = []
    for found in bundle.list_conversations():
        if only and str(found) not in only:
            continue
        try:
            item = bundle.load_conversation(found)
        except Exception:  # noqa: BLE001 - a bad file is the importer's to report
            continue
        (native if item.source_tool == tool else foreign).append(item)
    return native, foreign


def _cross_tool_settled(
    ui: UI, bundle: Bundle, adapter: Adapter, only: frozenset[str], allowed: bool
) -> bool:
    """Whether the import may go ahead, having said what crossing tools costs.

    Refused outright only when **everything** chosen is foreign: that import
    would write nothing, and exiting cleanly over it would tell a script it had
    worked. A bundle holding both goes ahead, and the adapter skips each
    foreign conversation by name, exactly as the screen's skip option does.
    """
    native, foreign = _split(bundle, adapter.name, only)
    if not foreign:
        return True
    sources = ", ".join(sorted({c.source_tool for c in foreign}))

    if not allowed:
        if native:
            ui.warn(
                f"{count_of(len(foreign), 'conversation')} came from {sources}, not "
                f"{adapter.display_name}, and will be skipped."
            )
            ui.info("Pass --allow-cross-tool to convert them as well.")
            return True
        ui.error(
            f"These conversations came from {sources}. Writing them into "
            f"{adapter.display_name} means converting them, and Ferry only does that "
            "when asked."
        )
        ui.info("Pass --allow-cross-tool to convert them.")
        ui.info("Add --dry-run as well to see what converting costs before anything is written.")
        return False

    impossible = [c for c in foreign if pair(c.source_tool, adapter.name).support == "unsupported"]
    convertible = [c for c in foreign if c not in impossible]
    if impossible:
        ui.warn(f"{len(impossible)} of them cannot be written into {adapter.display_name}.")
        ui.info(pair(impossible[0].source_tool, adapter.name).reason)
        if not convertible and not native:
            ui.error("There is nothing here Ferry can import.")
            return False
    if convertible:
        ui.blank()
        ui.info("Converting costs:")
        for note in _combined_loss(convertible, adapter.name):
            ui.info(f"  {note}")
    return True


def _warn_if_moved(ui: UI, bundle: Bundle, remap: tuple[tuple[str, str], ...]) -> None:
    """Say so when the bundle's home folder is not on this machine.

    Warned rather than refused. Leaving the paths as recorded is one of the
    screen's own answers, and a script that means it should not need a flag to
    say so -- but it should not find out afterwards, either.
    """
    if remap:
        return
    recorded = _recorded_home(bundle)
    here = str(Path.home())
    if not recorded or recorded == here or Path(recorded).is_dir():
        return
    ui.warn(f"This bundle was made on a machine whose home folder was {recorded}.")
    ui.info("That folder is not on this machine, so the paths inside are left as recorded.")
    ui.info(f'To read them as this machine\'s, pass --path-remap "{recorded}={here}"')


def import_bundle(
    ui: UI,
    *,
    bundle: str,
    tool: str,
    on_conflict: OnConflict = "skip",
    dry_run: bool = False,
    conversations: Sequence[str] = (),
    path_remap: Sequence[str] = (),
    allow_cross_tool: bool = False,
    mode: ConversionMode = "archive",
    passphrase: str | None = None,
) -> int:
    """Import a bundle into ``tool``. Returns an exit code.

    Backs up before writing, always: the screen offers no way to turn that off
    and neither does this (PLAN.md §6.1 allows it only behind a confirmation,
    and a script has nobody to confirm).
    """
    remap = _parse_remap(path_remap)
    if remap is None:
        ui.error("--path-remap takes OLD=NEW, the folder as recorded and the folder it is now.")
        return REFUSED

    verdict = bundle_at(bundle)
    if verdict.bundle is None:
        if verdict.nearby:
            ui.error(
                f"{Path(bundle).name} is not a bundle itself, but it holds "
                f"{count_of(len(verdict.nearby), 'bundle')}:"
            )
            for path in verdict.nearby[:_MAX_LISTED]:
                ui.info(f"  {path}")
            ui.info("Pass one of them to --bundle.")
        else:
            ui.error(verdict.problem or "--bundle names the bundle to import.")
        return REFUSED

    found = _found(ui, tool)
    if found is None:
        return FAILED
    adapter, _ = found

    with _readable(ui, verdict.bundle, passphrase or None) as root:
        if root is None:
            return REFUSED
        return _import_into(
            ui,
            adapter,
            root,
            ImportOptions(
                dry_run=dry_run,
                on_conflict=on_conflict,
                allow_cross_tool=allow_cross_tool,
                mode=mode,
                only=frozenset(conversations),
                path_remap=remap,
            ),
        )


def _import_into(ui: UI, adapter: Adapter, root: Path, options: ImportOptions) -> int:
    """Everything after the bundle is readable and the target is known."""
    try:
        opened = Bundle.open(root)
    except BundleError as exc:
        ui.error(str(exc))
        return REFUSED

    held = {str(found) for found in opened.list_conversations()}
    missing = sorted(options.only - held)
    if missing:
        ui.error(f"Not in this bundle: {', '.join(missing)}")
        ui.info("Run `ferry inspect` on it to see the conversations it holds.")
        return REFUSED

    tools = ", ".join(opened.manifest.tools_included) or "nothing"
    ui.info(f"{len(held)} conversations, from {tools}, made by {opened.manifest.created_by}")

    _warn_if_moved(ui, opened, options.path_remap)
    if not _cross_tool_settled(ui, opened, adapter, options.only, options.allow_cross_tool):
        ui.info("Nothing was written.")
        ui.blank()
        return REFUSED

    ui.blank()
    if options.dry_run:
        ui.info("Preview only. Nothing below is written.")
    else:
        ui.warn(f"This writes into your real {adapter.display_name} history.")

    kinds: Counter[str] = Counter()
    _report(
        ui,
        _tallied(adapter.import_(root, options), kinds),
        "conversations would be imported" if options.dry_run else "conversations imported",
        label="Previewing" if options.dry_run else "Importing",
        total=len(options.only) if options.only else len(held),
    )
    return FAILED if kinds["error"] else OK
