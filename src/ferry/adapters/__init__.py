"""Adapter package: the contract, and the registry of real implementations.

``base`` seeds :data:`~ferry.adapters.base.REGISTRY` with a stub per tool so the
detection screen can render honestly before any adapter exists. Each milestone
replaces one stub with the real thing, here -- importing the implementation from
``base`` itself would be a cycle, since every adapter imports ``base``.
"""

from ferry.adapters.base import (
    REGISTRY,
    Adapter,
    DetectResult,
    ExportEvent,
    ImportEvent,
    ImportOptions,
    get_adapter,
    list_adapters,
)
from ferry.adapters.claude_code import ClaudeCodeAdapter
from ferry.adapters.codex import CodexAdapter
from ferry.adapters.copilot import CopilotAdapter

REGISTRY["claude-code"] = ClaudeCodeAdapter()
REGISTRY["codex"] = CodexAdapter()
REGISTRY["copilot"] = CopilotAdapter()

__all__ = [
    "REGISTRY",
    "Adapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "CopilotAdapter",
    "DetectResult",
    "ExportEvent",
    "ImportEvent",
    "ImportOptions",
    "get_adapter",
    "list_adapters",
]
