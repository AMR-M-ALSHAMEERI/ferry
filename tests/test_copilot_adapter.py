"""The Copilot adapter: detect and export.

Export is the whole of what this adapter does today. The one property that
matters more than any other is the one every adapter shares: **reading a
person's history must not change it.**
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

import pytest

from ferry.adapters.copilot import paths as cp
from ferry.adapters.copilot.adapter import CopilotAdapter
from ferry.core import Bundle

FIXTURES = Path(__file__).parent / "fixtures" / "copilot"

WS_KEY = "0123456789abcdef0123456789abcdef"
IDS = {
    "basic": UUID("11111111-1111-4111-8111-111111111111"),
    "splice": UUID("22222222-2222-4222-8222-222222222222"),
    "edge": UUID("33333333-3333-4333-8333-333333333333"),
    "empty": UUID("44444444-4444-4444-8444-444444444444"),
}


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> Path:  # type: ignore[no-untyped-def]
    """A VS Code user directory holding every fixture."""
    user = tmp_path / "Code" / "User"
    workspace = user / "workspaceStorage" / WS_KEY / "chatSessions"
    workspace.mkdir(parents=True)
    empty_store = user / "globalStorage" / cp.EMPTY_WINDOW_DIR
    empty_store.mkdir(parents=True)

    for name in ("basic", "splice", "edge"):
        (workspace / f"{IDS[name]}.jsonl").write_bytes((FIXTURES / f"{name}.jsonl").read_bytes())
    (empty_store / f"{IDS['empty']}.jsonl").write_bytes((FIXTURES / "empty.jsonl").read_bytes())

    (user / "workspaceStorage" / WS_KEY / "workspace.json").write_text(
        '{"folder": "file:///c%3A/work/demo"}', encoding="utf-8"
    )
    monkeypatch.setenv(cp.USER_DIR_ENV, str(user))
    return user


# --------------------------------------------------------------------------
# detect
# --------------------------------------------------------------------------


def test_detect_finds_the_sessions(store: Path) -> None:
    result = CopilotAdapter().detect()

    assert result.installed
    assert result.conversation_count_estimate == 4


def test_detect_warns_that_the_format_is_undocumented(store: Path) -> None:
    """Required by PLAN.md M5: this storage is reverse-engineered and VS Code
    can change it in any release."""
    result = CopilotAdapter().detect()

    assert any("undocumented" in note for note in result.notes)
    assert any("1.134.0" in note for note in result.notes)


def test_detect_reports_not_installed_when_vs_code_is_absent(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(cp.USER_DIR_ENV, str(tmp_path / "nowhere"))

    result = CopilotAdapter().detect()

    assert not result.installed
    assert not result.notes[0].startswith("could not")


def test_detect_separates_a_user_with_no_chats_from_no_vs_code(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """Installed but unused is a different answer from not installed, and the
    person reading the screen needs to be able to tell."""
    user = tmp_path / "Code" / "User"
    user.mkdir(parents=True)
    monkeypatch.setenv(cp.USER_DIR_ENV, str(user))

    result = CopilotAdapter().detect()

    assert not result.installed
    assert any("no chat sessions yet" in note for note in result.notes)


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------


def test_export_never_modifies_the_source(store: Path) -> None:
    """The property that matters more than any other in this project."""
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for _, path in cp.session_files()}

    list(CopilotAdapter().export(store.parent.parent / "bundle"))

    after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for _, path in cp.session_files()}
    assert before == after


def test_export_writes_a_valid_bundle(store: Path, tmp_path: Path) -> None:
    dest = tmp_path / "bundle"

    list(CopilotAdapter().export(dest))

    bundle = Bundle.open(dest)
    assert bundle.validate() == []
    assert set(bundle.list_conversations()) == {IDS["basic"], IDS["splice"], IDS["edge"]}


def test_an_unused_chat_is_skipped_rather_than_exported_empty(store: Path, tmp_path: Path) -> None:
    """VS Code writes a session file whenever a panel opens. Exporting those as
    empty conversations would bury the real ones."""
    events = list(CopilotAdapter().export(tmp_path / "bundle"))

    skipped = [e for e in events if e.kind == "skipped"]
    assert any(str(IDS["empty"]) == e.conversation_id for e in skipped)


def test_the_workspace_is_recorded_without_disclosing_the_path_hash(
    store: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "bundle"
    list(CopilotAdapter().export(dest))

    conversation = Bundle.open(dest).load_conversation(IDS["basic"])
    assert conversation.workspace.path_hash == WS_KEY
    assert conversation.workspace.name == "demo"


def test_images_are_written_and_checksummed(store: Path, tmp_path: Path) -> None:
    dest = tmp_path / "bundle"

    list(CopilotAdapter().export(dest))

    conversation = Bundle.open(dest).load_conversation(IDS["edge"])
    assert len(conversation.attachments) == 1
    attachment = conversation.attachments[0]
    data = (dest / attachment.bundle_path).read_bytes()
    assert data[:4] == b"\x89PNG"
    assert hashlib.sha256(data).hexdigest() == attachment.sha256


def test_what_could_not_be_carried_is_reported(store: Path, tmp_path: Path) -> None:
    """An adapter that quietly drops block types is how a real one goes missing
    for a release."""
    events = list(CopilotAdapter().export(tmp_path / "bundle"))

    warnings = " ".join(e.message for e in events if e.kind == "warning")
    assert "thinking (no text stored)" in warnings
    assert "kind 7" in warnings


def test_a_second_export_into_the_same_bundle_skips_what_is_there(
    store: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "bundle"
    list(CopilotAdapter().export(dest))

    events = list(CopilotAdapter().export(dest))

    assert all(e.kind != "progress" for e in events)
    assert sum(1 for e in events if e.kind == "skipped") == 4


def test_export_reports_an_error_when_there_is_nothing_to_read(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setenv(cp.USER_DIR_ENV, str(tmp_path / "nowhere"))

    events = list(CopilotAdapter().export(tmp_path / "bundle"))

    assert [e.kind for e in events] == ["error"]


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------


def test_import_says_plainly_that_it_is_not_built(store: Path, tmp_path: Path) -> None:
    """A transcript written without its entry in chat.ChatSessionStore.index is
    a conversation VS Code will never show. Refusing beats writing that."""
    from ferry.adapters.base import ImportOptions

    events = list(CopilotAdapter().import_(tmp_path / "bundle", ImportOptions()))

    assert [e.kind for e in events] == ["error"]
    assert "not built yet" in events[0].message
