"""A converted conversation still says where it came from, months later.

Ferry built a `provenance` block on every cross-tool import and then discarded
it: assigned to an object that went out of scope, serialised nowhere, read by
nothing. `docs/CROSS-TOOL.md`, `docs/ADAPTERS.md` and `docs/ARCHITECTURE.md`
all promised it. The test named after it asserted on `assess()` and never
touched provenance -- **a test passing under a name for work it did not do**,
which is how a promise stays broken through a green suite.

So the tests here are deliberately about the *round trip* rather than about the
function that writes the record. Import a foreign conversation, export the
target again, and ask the conversation where it came from. That is the question
acceptance item A7b.8 asks, and it is the only phrasing the old defect could
not have survived.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from ferry.adapters.base import ImportOptions
from ferry.adapters.claude_code import ClaudeCodeAdapter
from ferry.core import Bundle, Manifest, SourceMachine
from ferry.core import provenance as provenance_store
from ferry.ucs import Conversation, Message, Provenance, TextBlock, ToolUseBlock, Workspace


def manifest() -> Manifest:
    return Manifest(
        created_at=datetime(2026, 9, 6, tzinfo=UTC),
        created_by="ferry test",
        source_machine=SourceMachine(hostname="test", os="win32", user_home="/home/test"),
    )


def conversation(tool: str) -> Conversation:
    now = datetime(2026, 9, 6, tzinfo=UTC)
    return Conversation(
        id=uuid4(),
        source_tool=tool,  # type: ignore[arg-type]
        workspace=Workspace(name="somewhere"),
        title="a converted conversation",
        created_at=now,
        updated_at=now,
        messages=[
            Message(role="user", content=[TextBlock(text="what did you do?")], timestamp=now),
            Message(
                role="assistant",
                content=[
                    TextBlock(text="this"),
                    ToolUseBlock(id="t1", name="CODE_ACTION", input={"path": "a.py"}),
                ],
                timestamp=now,
            ),
        ],
    )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The tool's store, redirected the way an adapter expects.

    **Ferry's own directory is redirected separately, through the process
    environment**, because that is where it is read from. An adapter's ``env``
    describes where *its tool* keeps things; handing it to Ferry's own store
    was the bug that put thirteen records in the developer's home.
    """
    monkeypatch.setenv(provenance_store.ROOT_ENV, str(tmp_path / "ferry"))
    return {"CLAUDE_CONFIG_DIR": str(tmp_path / "store")}


def _converted(tmp_path: Path, env: dict[str, str], item: Conversation) -> Conversation:
    """Import `item` into Claude Code, export it back, and return what came out."""
    incoming = tmp_path / "in"
    made = Bundle.create(incoming, manifest())
    made.add_conversation(item)
    adapter = ClaudeCodeAdapter(env)
    list(adapter.import_(incoming, ImportOptions(allow_cross_tool=True, backup=False)))

    out = tmp_path / "out"
    list(adapter.export(out))
    read = Bundle.open(out)
    found = [read.load_conversation(ref) for ref in read.list_conversations()]
    assert found, "the import wrote nothing to export"
    return found[0]


class TestTheRecordSurvivesTheRoundTrip:
    def test_a_converted_conversation_says_where_it_came_from(
        self, tmp_path: Path, env: dict[str, str]
    ) -> None:
        """The defect, phrased as the user meets it.

        Export the target tool after converting into it, and the conversation
        should still know it was not born there. It used to come back claiming
        to be a Claude Code conversation, with nothing anywhere to say otherwise.
        """
        came = _converted(tmp_path, env, conversation("antigravity"))

        assert came.provenance is not None, "a converted conversation lost its origin"
        assert came.provenance.original_tool == "antigravity"
        assert came.provenance.imported_into == "claude-code"
        assert came.provenance.lossy is True

    def test_the_record_lists_what_the_conversion_cost(
        self, tmp_path: Path, env: dict[str, str]
    ) -> None:
        """A provenance block saying less than the confirmation screen would be
        the more durable of the two documents disagreeing with the one someone
        actually read."""
        came = _converted(tmp_path, env, conversation("antigravity"))

        assert came.provenance is not None
        notes = " ".join(came.provenance.conversion_notes)
        assert notes.strip(), "the record admits no cost at all"
        assert "readable text" in notes

    def test_a_restore_is_not_marked_as_a_conversion(
        self, tmp_path: Path, env: dict[str, str]
    ) -> None:
        """Importing Claude Code into Claude Code is a restore, and a restore
        has no origin to declare. Stamping one would make every conversation
        look converted, which is the same failure from the other side."""
        came = _converted(tmp_path, env, conversation("claude-code"))

        assert came.provenance is None

    def test_nothing_is_written_outside_the_redirected_directory(
        self, tmp_path: Path, env: dict[str, str]
    ) -> None:
        """The store honours its override with no fallback.

        A helper that quietly reverted to the real home when the override
        looked wrong would write test data into the developer's own history,
        and would do it silently.
        """
        _converted(tmp_path, env, conversation("codex"))

        written = list((tmp_path / "ferry").rglob("*.json"))
        assert written, "the record did not land in the redirected directory"
        assert provenance_store.root() == tmp_path / "ferry"


class TestTheStoreItself:
    def test_a_missing_record_is_not_an_error(self, tmp_path: Path) -> None:
        """Most conversations are native and have nothing to declare."""
        env = {provenance_store.ROOT_ENV: str(tmp_path)}

        assert provenance_store.recall("claude-code", uuid4(), env) is None

    def test_an_unreadable_record_reads_as_absent(self, tmp_path: Path) -> None:
        """Ferry cannot say where the conversation came from, which is not a
        reason to refuse to export the conversation."""
        env = {provenance_store.ROOT_ENV: str(tmp_path)}
        conversation_id = uuid4()
        path = tmp_path / "claude-code" / f"{conversation_id}.json"
        path.parent.mkdir(parents=True)
        path.write_text("{ this is not json", encoding="utf-8")

        assert provenance_store.recall("claude-code", conversation_id, env) is None

    def test_a_record_round_trips(self, tmp_path: Path) -> None:
        env = {provenance_store.ROOT_ENV: str(tmp_path)}
        conversation_id = uuid4()
        stamp = Provenance(
            original_tool="codex",
            imported_into="claude-code",
            imported_at=datetime(2026, 9, 6, tzinfo=UTC),
            ferry_version="0.1.0",
            lossy=True,
            conversion_notes=["thinking signatures dropped"],
        )

        provenance_store.record("claude-code", conversation_id, stamp, env)
        back = provenance_store.recall("claude-code", conversation_id, env)

        assert back is not None
        assert back.original_tool == "codex"
        assert back.conversion_notes == ["thinking signatures dropped"]

    def test_two_tools_do_not_share_one_record(self, tmp_path: Path) -> None:
        """The same conversation id can exist in two tools at once -- Ferry
        keeps the id across a migration on purpose -- so the tool has to be part
        of the key or one import overwrites the other's history."""
        env = {provenance_store.ROOT_ENV: str(tmp_path)}
        conversation_id = uuid4()
        for tool, source in (("claude-code", "codex"), ("copilot", "antigravity")):
            provenance_store.record(
                tool,
                conversation_id,
                Provenance(
                    original_tool=source,  # type: ignore[arg-type]
                    imported_into=tool,  # type: ignore[arg-type]
                    imported_at=datetime(2026, 9, 6, tzinfo=UTC),
                    ferry_version="0.1.0",
                    lossy=True,
                    conversion_notes=[],
                ),
                env,
            )

        first = provenance_store.recall("claude-code", conversation_id, env)
        second = provenance_store.recall("copilot", conversation_id, env)

        assert first is not None and second is not None
        assert first.original_tool == "codex"
        assert second.original_tool == "antigravity"


def test_the_suite_never_touches_the_real_store() -> None:
    """A guard on the harness rather than on the product.

    Every test above redirects the store explicitly, but a *future* test doing
    a cross-tool import would write into the developer's own `~/.ferry` unless
    the conftest keeps the whole suite pointed elsewhere. That is precisely how
    the first thirteen records got there.
    """
    assert provenance_store.root() != Path.home() / ".ferry" / "provenance"
