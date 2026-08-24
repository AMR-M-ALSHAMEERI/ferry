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
from typing import Literal

__all__ = [
    "REGISTRY",
    "Adapter",
    "DetectResult",
    "ExportEvent",
    "ImportEvent",
    "ImportOptions",
    "NotImplementedAdapter",
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
    on_conflict: Literal["skip", "overwrite", "rename"] = "skip"
    allow_cross_tool: bool = False
    path_remap: tuple[tuple[str, str], ...] = ()


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
