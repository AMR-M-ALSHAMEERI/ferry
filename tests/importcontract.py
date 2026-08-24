"""Building the same situation in all four tools: a bundle, and a store to import into.

This exists so one test file can hold every adapter to the same promises. The
per-adapter test files each grew their own fixtures, and Copilot's import quietly
ignored ``dry_run``, ``backup`` and ``on_conflict`` for two milestones because
nothing ever asked it the questions the other three were being asked.

Each :class:`Case` knows only how to lay out a populated store, export it, and
hand back an adapter pointed at an empty one. Everything about *what should
happen* lives in ``test_import_contract.py``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from ferry.adapters.antigravity import AntigravityAdapter, schema
from ferry.adapters.antigravity import paths as ag_paths
from ferry.adapters.base import Adapter
from ferry.adapters.claude_code import ClaudeCodeAdapter
from ferry.adapters.claude_code import paths as cc_paths
from ferry.adapters.codex import CodexAdapter
from ferry.adapters.codex.paths import CODEX_HOME_ENV, rollout_name
from ferry.adapters.copilot import CopilotAdapter
from ferry.adapters.copilot import paths as cp_paths
from tests.antigravity_fixture import build_database

FIXTURES = Path(__file__).parent / "fixtures"

RENAME_IMPOSSIBLE = {"antigravity"}
"""Tools whose conversation id is written through storage Ferry cannot
re-identify. They must **refuse** rename, not quietly skip or overwrite."""


@dataclass
class Case:
    """One adapter, a bundle to import, and the store it will be written into."""

    name: str
    adapter: Adapter
    bundle: Path
    store: Path
    """Root of the destination store, for asserting that nothing was written."""

    written: Callable[[], list[Path]]
    """Every conversation file now in the destination store."""


def _export(adapter: Adapter, destination: Path) -> Path:
    for event in adapter.export(destination):
        if event.kind == "error":
            raise AssertionError(f"fixture export failed: {event.message}")
    return destination


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------

_CC_ID = UUID("aaaaaaaa-0000-4000-8000-000000000001")


def claude_code_case(tmp_path: Path) -> Case:
    source = tmp_path / "cc-source"
    project = source / "projects" / "C--Users-sample-Projects-widget"
    project.mkdir(parents=True)
    (project / f"{_CC_ID}.jsonl").write_bytes(
        (FIXTURES / "claude_code" / "basic.jsonl").read_bytes()
    )

    target = tmp_path / "cc-target"
    target.mkdir()
    env = {cc_paths.CONFIG_DIR_ENV: str(target)}
    bundle = _export(
        ClaudeCodeAdapter({cc_paths.CONFIG_DIR_ENV: str(source)}), tmp_path / "cc-bundle"
    )
    return Case(
        name="claude_code",
        adapter=ClaudeCodeAdapter(env),
        bundle=bundle,
        store=target,
        written=lambda: sorted(target.rglob("*.jsonl")),
    )


# --------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------

_CX_ID = UUID("019f1111-1111-7111-8111-111111111111")


def codex_case(tmp_path: Path) -> Case:
    source = tmp_path / "cx-source"
    day = source / "sessions" / "2026" / "08" / "01"
    day.mkdir(parents=True)
    (day / rollout_name(_CX_ID, "2026-08-01T09-00-00")).write_bytes(
        (FIXTURES / "codex" / "basic.jsonl").read_bytes()
    )

    target = tmp_path / "cx-target"
    target.mkdir()
    bundle = _export(CodexAdapter({CODEX_HOME_ENV: str(source)}), tmp_path / "cx-bundle")
    return Case(
        name="codex",
        adapter=CodexAdapter({CODEX_HOME_ENV: str(target)}),
        bundle=bundle,
        store=target,
        written=lambda: sorted((target / "sessions").rglob("*.jsonl")),
    )


# --------------------------------------------------------------------------
# Copilot Chat
# --------------------------------------------------------------------------

_CP_KEY = "0123456789abcdef0123456789abcdef"
_CP_ID = "11111111-1111-4111-8111-111111111111"


def copilot_case(tmp_path: Path) -> Case:
    user = tmp_path / "cp-source" / "Code" / "User"
    sessions = user / "workspaceStorage" / _CP_KEY / "chatSessions"
    sessions.mkdir(parents=True)
    (user / "globalStorage" / cp_paths.EMPTY_WINDOW_DIR).mkdir(parents=True)
    (sessions / f"{_CP_ID}.jsonl").write_bytes((FIXTURES / "copilot" / "basic.jsonl").read_bytes())
    (user / "workspaceStorage" / _CP_KEY / "workspace.json").write_text(
        '{"folder": "file:///c%3A/work/demo"}', encoding="utf-8"
    )

    target = tmp_path / "cp-target" / "Code" / "User"
    target.mkdir(parents=True)
    source_env = dict(os.environ, **{cp_paths.USER_DIR_ENV: str(user)})
    target_env = dict(os.environ, **{cp_paths.USER_DIR_ENV: str(target)})
    bundle = _export(CopilotAdapter(source_env), tmp_path / "cp-bundle")
    return Case(
        name="copilot",
        adapter=CopilotAdapter(target_env),
        bundle=bundle,
        store=target,
        written=lambda: sorted(target.rglob("*.jsonl")),
    )


# --------------------------------------------------------------------------
# Antigravity
# --------------------------------------------------------------------------

_AG_STEPS = [
    (0, schema.USER_INPUT, r"look at C:\Users\Dell\Work\main.py"),
    (1, schema.PLANNER_RESPONSE, r"I read C:/Users/Dell/Work/main.py"),
]
_AG_ID = "aaaaaaaa-1111-4111-8111-111111111111"
_AG_PROJECT = "99999999-9999-4999-8999-999999999999"


def antigravity_case(tmp_path: Path) -> Case:
    source = tmp_path / "ag-source" / "antigravity"
    build_database(source / "conversations" / f"{_AG_ID}.db", _AG_STEPS)
    projects = tmp_path / "ag-source" / "config" / "projects"
    projects.mkdir(parents=True)
    (projects / f"{_AG_PROJECT}.json").write_text(
        json.dumps({"id": _AG_PROJECT, "name": "Work"}), encoding="utf-8"
    )

    target = tmp_path / "ag-target" / "antigravity"
    bundle = _export(
        AntigravityAdapter({ag_paths.DATA_DIR_ENV: str(source)}), tmp_path / "ag-bundle"
    )
    return Case(
        name="antigravity",
        adapter=AntigravityAdapter({ag_paths.DATA_DIR_ENV: str(target)}),
        bundle=bundle,
        store=target,
        written=lambda: sorted(target.rglob("*.db")),
    )


BUILDERS: dict[str, Callable[[Path], Case]] = {
    "claude_code": claude_code_case,
    "codex": codex_case,
    "copilot": copilot_case,
    "antigravity": antigravity_case,
}
