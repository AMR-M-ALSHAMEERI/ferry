"""The Copilot adapter: detect and export.

Export is the whole of what this adapter does today. The one property that
matters more than any other is the one every adapter shares: **reading a
person's history must not change it.**
"""

from __future__ import annotations

import hashlib
import json
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


def test_detect_counts_conversations_not_session_files(store: Path) -> None:
    """VS Code writes a session file whenever a chat panel opens.

    On the machine this was written on that is 12 of 18 files, so counting
    files reported more than three times as many conversations as VS Code
    lists -- and the gap grows with use.
    """
    result = CopilotAdapter().detect()

    assert result.installed
    assert result.conversation_count_estimate == 3
    assert any("empty" in note for note in result.notes)


def test_a_new_vs_code_version_says_nothing_if_the_format_is_unchanged(
    store: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """VS Code updates monthly and almost never changes chat storage.

    A caveat tied to the version number would appear within weeks of release
    and never leave, and people would learn to scroll past it -- so it would
    still be there, unread, on the release that finally broke something. What
    is checked is the format; see tests/test_formatcheck.py.
    """
    monkeypatch.setattr(cp, "vscode_version", lambda env=None: "1.999.0")

    result = CopilotAdapter().detect()

    assert result.caveats == []
    assert result.version == "1.999.0"


def test_an_undetectable_version_is_not_a_caveat_on_its_own(
    store: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """Not knowing the version says nothing about the format.

    Ferry can read the transcripts and find them exactly as expected without
    ever learning what wrote them.
    """
    monkeypatch.setattr(cp, "vscode_version", lambda env=None: None)

    result = CopilotAdapter().detect()

    assert result.caveats == []


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

    # Notes and warnings both count here: what matters is that it is said, and
    # these are remarks about the format rather than fidelity losses.
    said = " ".join(e.message for e in events if e.kind in ("note", "warning"))
    assert "thinking (no text stored)" in said
    assert "kind 7" in said


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


def test_a_conversation_from_another_tool_needs_asking_for(  # type: ignore[no-untyped-def]
    tmp_path: Path,
    target,
    manifest,
    conversation,
) -> None:
    """Cross-tool is refused unless it is asked for, exactly as elsewhere.

    Copilot was ``unsupported`` as a target until M7b.2 Phase 2, and the reason
    was wrong twice on the way (#193, #203). It is supported now because the
    document is built rather than replayed, so what is left to protect is the
    rule that nobody converts anything by accident.
    """
    from ferry.adapters.base import ImportOptions

    dest = tmp_path / "foreign"
    bundle = Bundle.create(dest, manifest)
    bundle.add_conversation(conversation)

    events = list(CopilotAdapter(target).import_(dest, ImportOptions()))

    assert sum(1 for e in events if e.kind == "progress") == 0
    skipped = [e.message for e in events if e.kind == "skipped"]
    assert any("must be asked for explicitly" in message for message in skipped)
    # Never a milestone name: "this arrives at M7b" was true until M7b arrived.
    assert not any("M7b" in message for message in skipped)


def test_import_reports_a_bundle_it_cannot_open(tmp_path: Path, target) -> None:  # type: ignore[no-untyped-def]
    from ferry.adapters.base import ImportOptions

    events = list(CopilotAdapter(target).import_(tmp_path / "nothing", ImportOptions()))

    assert [e.kind for e in events] == ["error"]


def test_a_duplicate_conversation_id_is_skipped_quietly_when_it_agrees(
    store: Path, tmp_path: Path
) -> None:
    """The same session id really does appear under more than one workspace.
    One conversation on the probe machine exists in two byte-different files
    holding identical content, and skipping the second is correct."""
    workspace_b = cp.workspace_storage() / "ffffffffffffffffffffffffffffffff" / "chatSessions"
    workspace_b.mkdir(parents=True)
    original = cp.workspace_storage() / WS_KEY / "chatSessions" / f"{IDS['basic']}.jsonl"
    (workspace_b / f"{IDS['basic']}.jsonl").write_bytes(original.read_bytes())

    events = list(CopilotAdapter().export(tmp_path / "bundle"))

    skipped = [e for e in events if e.kind == "skipped" and e.conversation_id == str(IDS["basic"])]
    assert len(skipped) == 1
    assert skipped[0].message == "already in bundle"
    assert not [e for e in events if e.kind == "warning" and "longer copy" in e.message]


def test_a_longer_duplicate_is_reported_rather_than_swallowed(store: Path, tmp_path: Path) -> None:
    """Skipping by id is only right while the two copies agree. If the one
    being skipped holds more messages, the bundle keeps the shorter one and
    turns are lost without a word."""
    import json

    long_dir = cp.workspace_storage() / "ffffffffffffffffffffffffffffffff" / "chatSessions"
    long_dir.mkdir(parents=True)
    original = cp.workspace_storage() / WS_KEY / "chatSessions" / f"{IDS['basic']}.jsonl"
    extra = {
        "kind": 2,
        "k": ["requests"],
        "v": [
            {
                "requestId": "r-extra",
                "message": {"text": "one more question", "parts": []},
                "response": [{"value": "and one more answer.", "supportHtml": False}],
            }
        ],
    }
    (long_dir / f"{IDS['basic']}.jsonl").write_text(
        original.read_text(encoding="utf-8") + json.dumps(extra) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    events = list(CopilotAdapter().export(tmp_path / "bundle"))

    warnings = [e for e in events if e.kind == "warning" and "longer copy" in e.message]
    assert len(warnings) == 1
    assert "not carried" in warnings[0].message
    skipped = [e for e in events if e.kind == "skipped" and e.conversation_id == str(IDS["basic"])]
    assert "longer copy exists" in skipped[0].message


def test_the_files_a_tool_named_survive_a_round_trip(tmp_path: Path, monkeypatch, target) -> None:  # type: ignore[no-untyped-def]
    """The reader now carries `invocationMessage.uris` into the call's input.

    The writer rebuilds a transcript from `source_raw` and never reads `input`,
    so this should hold untouched -- **which is exactly why it is asserted
    rather than assumed.** A round trip that quietly stopped being faithful is
    the kind of breakage no unit test above would notice.
    """
    from ferry.adapters.base import ImportOptions
    from ferry.adapters.copilot.reader import URIS_KEY, read_session

    session_id = UUID("77777777-8888-4999-8aaa-bbbbbbbbbbbb")
    user = tmp_path / "source" / "Code" / "User"
    chats = user / "workspaceStorage" / WS_KEY / "chatSessions"
    chats.mkdir(parents=True)
    (chats / f"{session_id}.jsonl").write_bytes((FIXTURES / "uris.jsonl").read_bytes())
    monkeypatch.setenv(cp.USER_DIR_ENV, str(user))

    exported = tmp_path / "bundle"
    list(CopilotAdapter().export(exported))
    original = Bundle.open(exported).load_conversation(session_id)
    assert any(
        URIS_KEY in b.input for m in original.messages for b in m.content if b.type == "tool_use"
    )

    list(CopilotAdapter(target).import_(exported, ImportOptions()))

    written = cp.session_files(target)
    assert len(written) == 1
    key, path = written[0]
    rebuilt = read_session(path, key, target).conversation
    assert rebuilt is not None
    assert [[b.model_dump() for b in m.content] for m in rebuilt.messages] == [
        [b.model_dump() for b in m.content] for m in original.messages
    ]


class TestBuildingADocumentForAForeignConversation:
    """M7b.2 Phase 2: writing a conversation Copilot never had.

    The shape asserted here is the one VS Code was **measured** to accept
    (PROGRESS #203, #204): it listed the conversation, took the title out of
    the document rather than the index, and rendered the reply. A real Copilot
    request carries 24 fields including the extension's own manifest, token
    counts and credits. None of that is written, because writing it would mean
    inventing telemetry about a conversation that never happened in VS Code.
    """

    @staticmethod
    def _written(tmp_path: Path, target, manifest, conversation):  # type: ignore[no-untyped-def]
        from ferry.adapters.base import ImportOptions

        dest = tmp_path / "foreign"
        bundle = Bundle.create(dest, manifest)
        bundle.add_conversation(conversation)
        events = list(CopilotAdapter(target).import_(dest, ImportOptions(allow_cross_tool=True)))
        # Asked of the adapter's own path resolver, and looked up by id. Both
        # halves were wrong first: a hand-built path found the *real* store on
        # this machine, and "the first file" would have read one of the
        # fixture's other sessions and asserted nothing.
        written = cp.empty_window_dir(target) / f"{conversation.id}.jsonl"
        assert written.is_file(), "nothing was written for this conversation"
        record = json.loads(written.read_text(encoding="utf-8").splitlines()[0])
        return record["v"], events

    def test_the_document_has_the_shape_vs_code_accepted(  # type: ignore[no-untyped-def]
        self, tmp_path: Path, target, manifest, conversation
    ) -> None:
        document, _ = self._written(tmp_path, target, manifest, conversation)

        assert document["version"] == 3
        assert document["sessionId"] == str(conversation.id)
        assert isinstance(document["creationDate"], int)
        assert document["requests"], "a conversation with messages has requests"
        first = document["requests"][0]
        assert set(first) >= {"requestId", "responseId", "message", "response"}
        assert "parts" in first["message"]

    def test_the_question_is_carried_where_vs_code_draws_it(  # type: ignore[no-untyped-def]
        self, tmp_path: Path, target, manifest, conversation
    ) -> None:
        """The defect this test was written for, and the hole it fell through.

        Ferry wrote the question into `message.text` and left `parts` empty.
        VS Code accepted the document and titled the chat from `text`, so every
        check passed -- while on screen every question bubble was blank and only
        the answers showed. The old assertion here was `"parts" in message`,
        which an empty list satisfies. **Checking that a field exists is not
        checking that it carries anything**, and the difference was the whole
        conversation from the reader's side.
        """
        document, _ = self._written(tmp_path, target, manifest, conversation)

        asked = [r for r in document["requests"] if r["message"]["text"]]
        assert asked, "the fixture must contain at least one question"
        for request in asked:
            message = request["message"]
            parts = message["parts"]
            assert len(parts) == 1, "VS Code writes exactly one text part per question"
            part = parts[0]
            assert part["kind"] == "text"
            assert part["text"] == message["text"], "the part is the question, not a summary"
            assert part["range"] == {"start": 0, "endExclusive": len(message["text"])}
            assert set(part["editorRange"]) == {
                "startLineNumber",
                "startColumn",
                "endLineNumber",
                "endColumn",
            }

    def test_no_telemetry_is_invented(  # type: ignore[no-untyped-def]
        self, tmp_path: Path, target, manifest, conversation
    ) -> None:
        """The point of the experiment, asserted.

        A token count, a credit figure or a model id written here would be a
        number Ferry made up about work that was never done in VS Code.
        """
        document, _ = self._written(tmp_path, target, manifest, conversation)

        invented = {
            "promptTokens",
            "completionTokens",
            "copilotCredits",
            "modelId",
            "modelState",
            "agent",
            "promptTokenDetails",
            "elapsedMs",
        }
        for request in document["requests"]:
            assert not (set(request) & invented), f"invented: {set(request) & invented}"

    def test_it_is_not_presented_as_copilot_s_own(  # type: ignore[no-untyped-def]
        self, tmp_path: Path, target, manifest, conversation
    ) -> None:
        """Ferry's standing rule, in the one field that names the responder."""
        document, _ = self._written(tmp_path, target, manifest, conversation)

        assert document["responderUsername"] == conversation.source_tool
        assert document["responderUsername"] != "GitHub Copilot"

    def test_the_conversation_s_own_title_survives(  # type: ignore[no-untyped-def]
        self, tmp_path: Path, target, manifest, conversation
    ) -> None:
        """Without `customTitle`, VS Code names the chat after the first
        message. That is what it did to the bare probe, and it is how we
        learned it had parsed the document at all."""
        document, _ = self._written(tmp_path, target, manifest, conversation)

        assert document["customTitle"] == conversation.title

    def test_the_import_says_what_it_cost(  # type: ignore[no-untyped-def]
        self, tmp_path: Path, target, manifest, conversation
    ) -> None:
        _, events = self._written(tmp_path, target, manifest, conversation)

        assert any("readable text" in e.message for e in events if e.kind == "warning")

    def test_the_same_conversation_imported_twice_gets_the_same_ids(  # type: ignore[no-untyped-def]
        self, tmp_path: Path, target, manifest, conversation
    ) -> None:
        """Random ids would make a re-import look like a different
        conversation to anything comparing two exports."""
        from ferry.adapters.copilot.writer import synthesize_document

        first = synthesize_document(conversation)
        second = synthesize_document(conversation)

        assert [r["requestId"] for r in first["requests"]] == [
            r["requestId"] for r in second["requests"]
        ]
