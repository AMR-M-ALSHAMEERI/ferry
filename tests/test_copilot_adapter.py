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
    """Required by PLAN.md M5's exit criteria, and a caveat rather than a note:
    everything Ferry knows about this format came off one machine, so the user
    is told on every scan instead of once in detail they must go looking for."""
    result = CopilotAdapter().detect()

    assert any("undocumented" in caveat for caveat in result.caveats)
    assert any("1.134.0" in caveat for caveat in result.caveats)


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


@pytest.fixture
def exported(store: Path, tmp_path: Path) -> Path:
    dest = tmp_path / "bundle"
    list(CopilotAdapter().export(dest))
    return dest


@pytest.fixture
def target(tmp_path: Path) -> dict[str, str]:
    """A separate VS Code user directory to import into."""
    import os

    user = tmp_path / "target" / "Code" / "User"
    user.mkdir(parents=True)
    env = dict(os.environ)
    env[cp.USER_DIR_ENV] = str(user)
    return env


def test_import_writes_the_transcript_and_lists_it(exported: Path, target) -> None:  # type: ignore[no-untyped-def]
    """Both halves, or neither. A transcript VS Code does not list is a
    conversation the user cannot reach and cannot discover exists."""
    from ferry.adapters.base import ImportOptions
    from ferry.adapters.copilot.writer import index_lists

    events = list(CopilotAdapter(target).import_(exported, ImportOptions()))

    assert sum(1 for e in events if e.kind == "progress") == 3
    written = cp.session_files(target)
    assert len(written) == 3
    listed = index_lists(cp.global_storage(target) / "state.vscdb")
    assert {path.stem for _, path in written} <= listed


def test_import_round_trips_the_conversation_unchanged(exported: Path, target) -> None:  # type: ignore[no-untyped-def]
    """The check that catches what no unit test does: read what was written and
    compare it with what was exported, block for block."""
    from ferry.adapters.base import ImportOptions
    from ferry.adapters.copilot.reader import read_session

    list(CopilotAdapter(target).import_(exported, ImportOptions()))

    bundle = Bundle.open(exported)
    for key, path in cp.session_files(target):
        rebuilt = read_session(path, key, target).conversation
        assert rebuilt is not None
        original = bundle.load_conversation(rebuilt.id)
        assert rebuilt.title == original.title
        assert [[b.model_dump() for b in m.content] for m in rebuilt.messages] == [
            [b.model_dump() for b in m.content] for m in original.messages
        ]


def test_importing_twice_does_not_duplicate(exported: Path, target) -> None:  # type: ignore[no-untyped-def]
    from ferry.adapters.base import ImportOptions

    list(CopilotAdapter(target).import_(exported, ImportOptions()))
    events = list(CopilotAdapter(target).import_(exported, ImportOptions()))

    assert sum(1 for e in events if e.kind == "progress") == 0
    assert sum(1 for e in events if e.kind == "skipped") == 3
    assert len(cp.session_files(target)) == 3


def test_import_does_not_invent_a_workspace_that_is_not_here(exported: Path, target) -> None:  # type: ignore[no-untyped-def]
    """The recorded key is a digest of a folder path *and its creation time* on
    another machine. Creating a directory to make it fit would produce a
    workspace VS Code has never heard of, so these land in the no-folder list."""
    from ferry.adapters.base import ImportOptions

    list(CopilotAdapter(target).import_(exported, ImportOptions()))

    assert all(key == "" for key, _ in cp.session_files(target))
    assert not (cp.workspace_storage(target) / WS_KEY).exists()


def test_import_returns_a_conversation_to_its_workspace_when_it_is_here(
    exported: Path,
    target,  # type: ignore[no-untyped-def]
) -> None:
    from ferry.adapters.base import ImportOptions

    (cp.workspace_storage(target) / WS_KEY).mkdir(parents=True)

    list(CopilotAdapter(target).import_(exported, ImportOptions()))

    assert all(key == WS_KEY for key, _ in cp.session_files(target))


def test_an_existing_chat_list_is_added_to_not_replaced(exported: Path, target) -> None:  # type: ignore[no-untyped-def]
    """The index is one JSON blob holding every conversation. A careless write
    deletes the user's chat list while appearing to add to it."""
    import json
    import sqlite3

    from ferry.adapters.base import ImportOptions
    from ferry.adapters.copilot.writer import INDEX_KEY, index_lists

    database = cp.global_storage(target) / "state.vscdb"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    with connection:
        connection.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)")
        connection.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            (
                INDEX_KEY,
                json.dumps({"version": 1, "entries": {"keep-me": {"sessionId": "keep-me"}}}),
            ),
        )
    connection.close()

    list(CopilotAdapter(target).import_(exported, ImportOptions()))

    assert "keep-me" in index_lists(database)


def test_a_conversation_from_another_tool_is_skipped_not_mangled(
    tmp_path: Path,
    target,  # type: ignore[no-untyped-def]
    manifest,
    conversation,
) -> None:
    """Cross-tool import is M7b. Writing a Claude Code conversation into
    Copilot's format now would produce something neither tool can read."""
    from ferry.adapters.base import ImportOptions

    dest = tmp_path / "foreign"
    bundle = Bundle.create(dest, manifest)
    bundle.add_conversation(conversation)

    events = list(CopilotAdapter(target).import_(dest, ImportOptions()))

    assert sum(1 for e in events if e.kind == "progress") == 0
    assert any("M7b" in e.message for e in events if e.kind == "skipped")


def test_import_reports_a_bundle_it_cannot_open(tmp_path: Path, target) -> None:  # type: ignore[no-untyped-def]
    from ferry.adapters.base import ImportOptions

    events = list(CopilotAdapter(target).import_(tmp_path / "nothing", ImportOptions()))

    assert [e.kind for e in events] == ["error"]
