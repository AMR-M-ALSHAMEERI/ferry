"""The GitHub Copilot Chat adapter.

VS Code stores a chat as a log of edits rather than a document, so reading one
means replaying it -- see :mod:`ferry.adapters.copilot.deltas`, which is the
riskiest part of this adapter and is built and tested on its own.
"""

from ferry.adapters.copilot.adapter import TOOL, CopilotAdapter

__all__ = ["TOOL", "CopilotAdapter"]
