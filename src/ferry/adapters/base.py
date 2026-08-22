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

EventKind = Literal["started", "progress", "skipped", "warning", "error", "done"]


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


@dataclass(frozen=True)
class ExportEvent:
    """Progress signal emitted while conversations are being exported."""

    kind: EventKind
    conversation_id: str | None = None
    message: str = ""


@dataclass(frozen=True)
class ImportEvent:
    """Progress signal emitted while conversations are being imported."""

    kind: EventKind
    conversation_id: str | None = None
    message: str = ""


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
