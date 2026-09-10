"""The adapter contract, event types, and the adapter registry.

This is the interface defined in PLAN.md §4. Every tool Ferry supports
implements :class:`Adapter`; the CLI never touches a tool's storage directly.

At M2 all four adapters are stubs that report themselves as not installed —
the wiring exists so the interface can be built and tested, but no tool-specific
logic is written until each adapter's own milestone. The stubs report honestly
rather than inventing conversation counts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ferry.core.provenance import Written

__all__ = [
    "OnConflict",
    "REGISTRY",
    "Adapter",
    "DetectResult",
    "ExportEvent",
    "ImportEvent",
    "ImportOptions",
    "NotImplementedAdapter",
    "RemovalBlocked",
    "RemoveEvent",
    "RemoveOptions",
    "get_adapter",
    "list_adapters",
]

EventKind = Literal["started", "progress", "skipped", "note", "warning", "error", "done"]
"""What an event is, which decides how loudly it is shown.

``note`` and ``warning`` are separated because they were not, and everything
came out looking alarming. A note is a fact about the format the user may want
to know -- *"22 images were attached; Antigravity does not record which message
they belonged to"*. A warning is something that may have cost them fidelity --
*"this conversation contains a step type Ferry has no name for"*. Rendering the
first as the second teaches people that the warning marker means nothing.

Adapters should emit these **already aggregated**, once per export, rather than
once per conversation. The same sentence repeated four times with different
numbers in front of it is not four pieces of information.
"""


@dataclass(frozen=True)
class DetectResult:
    """What an adapter found when it looked for its tool.

    ``detect()`` must never raise, so this doubles as the error channel: a
    failed probe returns ``installed=False`` with the reason in ``notes``.
    """

    installed: bool
    version: str | None = None
    data_paths: list[Path] = field(default_factory=list)
    conversation_count_estimate: int = 0
    notes: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    """Things the user must be told before trusting this adapter.

    Separate from ``notes`` because notes are detail shown on request, while a
    caveat is shown every time. Copilot Chat's storage is reverse-engineered
    and VS Code can change it in any release; a user has to know that before
    they rely on the export, not after.
    """


@dataclass(frozen=True)
class ExportEvent:
    """Progress signal emitted while conversations are being exported."""

    kind: EventKind
    conversation_id: str | None = None
    message: str = ""

    total: int | None = None
    """How many items this run will actually work through, on a ``started`` event.

    The screen otherwise sizes its bar from ``detect()``, which counts
    *conversations* -- and an adapter can have more work than that. Antigravity
    carries a subagent trajectory for each conversation that spawned one, so
    two conversations are six pieces of work, and the bar read "6/2".

    The adapter knows; ``detect()`` guesses. Left ``None`` by adapters whose
    work is one item per conversation.
    """


@dataclass(frozen=True)
class ImportEvent:
    """Progress signal emitted while conversations are being imported."""

    kind: EventKind
    conversation_id: str | None = None
    message: str = ""

    total: int | None = None
    """How many items this run will actually work through, on a ``started`` event.

    The screen otherwise sizes its bar from ``detect()``, which counts
    *conversations* -- and an adapter can have more work than that. Antigravity
    carries a subagent trajectory for each conversation that spawned one, so
    two conversations are six pieces of work, and the bar read "6/2".

    The adapter knows; ``detect()`` guesses. Left ``None`` by adapters whose
    work is one item per conversation.
    """


FERRY_WROTE_IT = "ferry-"
"""How a record says Ferry wrote it, rather than the tool.

Two adapters report the tool's version by reading it out of the newest
conversation, because the app is installed somewhere different on every
platform and a version that never wrote anything is not the version that
produced the history. Ferry stamps what it writes honestly -- `ferry-0.1.0` --
and the consequence was that importing into Codex made Ferry report **Codex's
version as `ferry-0.1.0`**: the tool reading its own handwriting back as a
measurement of somebody else.

Anything carrying this prefix is Ferry's, and is skipped when asking what
version of a tool wrote a person's history.
"""


ConversionMode = Literal["archive", "continue"]
"""How much of a conversation crossing tools is given up.

``archive`` keeps everything and is the default -- it is what Ferry has always
done, and a restore never sees this at all. ``continue`` gives up more on
purpose so the target's assistant can carry the conversation on: an unsigned
thinking block is rejected outright by the vendor that issued the signature, and
a tool call naming a tool the target does not have describes something that
cannot have happened there.

Ignored entirely when the source and target are the same tool. A restore is not
a conversion and has nothing to trade.
"""


OnConflict = Literal["skip", "overwrite", "rename"]
"""What to do about a conversation the target tool already has.

``rename`` means importing it as a separate conversation under a new id;
see :mod:`ferry.adapters.conflict` for why a new *filename* is not a
coherent answer.
"""


@dataclass(frozen=True)
class ImportOptions:
    """Caller-controlled behaviour for an import.

    ``backup`` defaults to on deliberately: Ferry writes into a user's real
    conversation history, and losing that is the worst failure this project has
    (PLAN.md §6.1).

    ``path_remap`` exists because a conversation carries the absolute path of
    the directory it happened in, and that path is usually wrong on the machine
    it is being restored to. Rules are ``(old_prefix, new_prefix)`` pairs
    applied in order, first match wins; an unmatched path is left as it was.
    """

    backup: bool = True
    dry_run: bool = False
    on_conflict: OnConflict = "skip"
    allow_cross_tool: bool = False
    mode: ConversionMode = "archive"
    trust_folders: bool = False
    """Let the target open the folders written into, where it needs telling.

    Claude Code refuses to open a conversation in a folder absent from its
    trust map, so a restore onto a new machine is unopenable until every folder
    is trusted by hand. Ferry can do that, and **the default is off**: writing
    into another tool's configuration is a security decision, so a programmatic
    import never takes it. Only the interactive screen offers it, with the
    cursor already on yes.
    """

    only: frozenset[str] = frozenset()
    """Conversation ids to import, as strings. Empty means **all of them**.

    Empty rather than ``None`` for "everything" because that is the ordinary
    case and it should be the cheap one to express. An adapter honouring this
    filters and says nothing: a conversation the person did not choose is not a
    conversation that was skipped, and reporting it as skipped would fill the
    screen with notices about a decision they already made.
    """

    path_remap: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RemoveEvent:
    """Progress signal emitted while conversations Ferry wrote are being deleted.

    The same shape as the other two, so the screen that renders an import
    renders this too. A delete that reported itself some other way would be the
    one place where a warning could go unshown.
    """

    kind: EventKind
    conversation_id: str | None = None
    message: str = ""

    total: int | None = None
    """How many conversations this run will delete, on a ``started`` event."""


@dataclass(frozen=True)
class RemoveOptions:
    """Caller-controlled behaviour for deleting what Ferry wrote.

    ``backup`` defaults to on for the same reason an import's does: this removes
    things from a person's real history. Everything it can remove also came out
    of a bundle, but that bundle may be long gone.
    """

    dry_run: bool = False
    backup: bool = True
    only: frozenset[str] = frozenset()
    """Record ids to delete, as strings. Empty means every one that can go."""


class RemovalBlocked(RuntimeError):
    """The entry that makes a tool list a conversation could not be taken out.

    Raised by :meth:`Adapter.unlist`, and it stops that conversation's delete
    **before the file is touched**. A file gone but still listed is an empty
    conversation in someone's list; a file still there and still listed is
    exactly what they had, which is the right state to fail into.
    """


class Adapter(ABC):
    """Per-tool reader and writer of conversation history.

    Implementations live in ``ferry.adapters.<tool>`` and are the only code that
    knows where a tool keeps its data or what shape that data is in.
    """

    name: str
    display_name: str

    @abstractmethod
    def detect(self) -> DetectResult:
        """Look for the tool and report what was found.

        Must never raise. If the probe fails for any reason, return
        ``installed=False`` and explain why in ``notes`` — a broken probe for
        one tool must not stop the user migrating the others.
        """

    @abstractmethod
    def export(self, dest_bundle_dir: Path) -> Iterator[ExportEvent]:
        """Read conversations and write them into ``dest_bundle_dir`` as UCS.

        Must be resumable: re-running after an interruption skips conversations
        already written. Must never modify the source tool's data.
        """

    @abstractmethod
    def import_(self, bundle_dir: Path, options: ImportOptions) -> Iterator[ImportEvent]:
        """Write UCS conversations from ``bundle_dir`` into the tool's storage.

        Trailing underscore because ``import`` is a Python keyword. Note the
        contract does not require the UCS to have come from this same tool —
        that is what makes cross-tool migration (M7b) possible.
        """

    def unopenable(self, bundle_dir: Path, options: ImportOptions) -> list[str]:
        """Places this tool will refuse to open until it is told to allow them.

        Writing a conversation is not always the whole job. Claude Code will
        not open a folder absent from its trust map, and reports those folders
        here so the flow can offer to sort it out **before** anyone meets the
        refusal. Purely a question: nothing is written by asking.

        The default is an empty list, which is the honest answer for a tool
        with no such gate and for a tool whose gate has not been measured yet.
        Never raises; an adapter that cannot tell reports nothing rather than
        blocking an import over a question it could not answer.
        """
        return []

    # Deleting what Ferry wrote; see ferry.adapters.removal. Every default makes
    # a delete do nothing, so an adapter that has not described where it writes
    # can never be asked to remove anything.

    def written_roots(self) -> list[Path]:
        """Where this adapter writes conversations.

        A delete only touches files inside these. Ferry's records name the file
        each import wrote, and a record naming one anywhere else -- written
        while this tool was pointed at a different store -- is left alone rather
        than followed out of the store being cleaned.
        """
        return []

    def listing(self, written: Path) -> Path | None:
        """The file holding the entry that makes the tool list ``written``.

        ``None`` for a tool that lists whatever is on disk. Named separately
        from :meth:`unlist` so a delete can back it up before changing it, and
        say in a preview what it would touch.
        """
        return None

    def unlist(self, written: Path) -> None:
        """Take out the entry that makes the tool list ``written``.

        The other half of every import: a file with no entry is invisible, and
        an entry with no file is an empty conversation in the list. Must be
        safe to call when the entry is already gone, because a delete that
        stopped halfway is finished by running it again.

        Raises:
            RemovalBlocked: If the entry could not be taken out.
        """
        # Nothing to take out by default: a tool that lists whatever is on
        # disk stops listing a conversation when its file goes.
        return None

    def in_use(self) -> str | None:
        """Why a delete has to wait, or ``None`` if it need not.

        A tool that keeps its list in memory and writes it back when it closes
        would put the entry straight back, so it has to be closed first. The
        answer is a sentence for the person rather than a flag.
        """
        return None

    def used_since(self, written: Path) -> bool:
        """Whether anything beside ``written`` shows it has been used.

        The fingerprint covers the one file Ferry wrote. A database can take new
        work into a journal beside it and leave the main file byte for byte the
        same, and a conversation carried on that way belongs to the person.
        """
        return False

    def only_opened(self, written: Path, was: Written) -> bool:
        """Whether ``written`` differs from what Ferry wrote only by being opened.

        Asked only when the checksum no longer matches. Some files are changed
        by the act of looking: SQLite rewrites part of a database's header when
        it opens one. A conversation someone opened to check it had arrived is
        not one they worked in, and refusing to delete it on that account made
        the delete refuse the one thing it was asked for. The default is
        ``False``: without proof, a changed file is the person's.
        """
        return False

    def companions(self, written: Path) -> list[Path]:
        """Files that belong to ``written`` and go when it goes."""
        return []


class NotImplementedAdapter(Adapter):
    """Placeholder for a tool whose adapter has not been built yet.

    Reports itself as not installed with an honest note, so the detection
    screen can render truthfully at M2 without inventing data. Each of these is
    replaced by a real implementation at that tool's own milestone.
    """

    def __init__(self, name: str, display_name: str, milestone: str) -> None:
        self.name = name
        self.display_name = display_name
        self.milestone = milestone

    def detect(self) -> DetectResult:
        return DetectResult(
            installed=False,
            notes=[f"adapter not yet implemented (arrives at {self.milestone})"],
        )

    def export(self, dest_bundle_dir: Path) -> Iterator[ExportEvent]:
        raise NotImplementedError(f"{self.display_name} export arrives at {self.milestone}")

    def import_(self, bundle_dir: Path, options: ImportOptions) -> Iterator[ImportEvent]:
        raise NotImplementedError(f"{self.display_name} import arrives at {self.milestone}")


REGISTRY: dict[str, Adapter] = {
    "claude-code": NotImplementedAdapter("claude-code", "Claude Code", "M3"),
    "codex": NotImplementedAdapter("codex", "OpenAI Codex", "M4"),
    "copilot": NotImplementedAdapter("copilot", "GitHub Copilot Chat", "M5"),
    "antigravity": NotImplementedAdapter("antigravity", "Antigravity", "M6"),
}
"""All known adapters, in the order they are implemented and displayed.

Seeded with stubs and overwritten by ``ferry.adapters.__init__`` as each real
adapter arrives. The registration lives there rather than here so this module
stays free of adapter imports -- every adapter imports *this* file, so an
import in the other direction would be a cycle.
"""


def list_adapters() -> list[Adapter]:
    """Every registered adapter, in registry order."""
    return list(REGISTRY.values())


def get_adapter(name: str) -> Adapter:
    """Look up one adapter by its short name.

    Raises:
        KeyError: If no adapter is registered under that name.
    """
    return REGISTRY[name]
