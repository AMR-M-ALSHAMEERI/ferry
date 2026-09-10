"""Deleting what Ferry imported, from all four tools, and nothing else.

Two promises carry the weight. **Only what Ferry wrote, and only while it is
still exactly what Ferry wrote**: a conversation someone carried on is theirs.
And **both halves of the write go**: three of the four tools list a
conversation somewhere other than its file, and a delete that took only the
file would leave an empty conversation in the list.

Every behaviour test runs against all four targets. The import contract exists
because Copilot once ignored three options for two milestones while the other
adapters were being asked about them; this file is not going to repeat that.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from ferry.adapters.antigravity import AntigravityAdapter, wire
from ferry.adapters.antigravity import paths as ag_paths
from ferry.adapters.antigravity.index import AntigravityIndexLocked, drop_entry, entry_for
from ferry.adapters.antigravity.opened import opened_not_changed
from ferry.adapters.base import (
    Adapter,
    DetectResult,
    ImportOptions,
    RemovalBlocked,
    RemoveOptions,
)
from ferry.adapters.claude_code import SIDECAR_SUBDIR, ClaudeCodeAdapter
from ferry.adapters.claude_code import paths as cc_paths
from ferry.adapters.codex import CodexAdapter
from ferry.adapters.codex.index import drop_thread_row, thread_row, upsert_thread_row
from ferry.adapters.codex.paths import CODEX_HOME_ENV
from ferry.adapters.copilot import CopilotAdapter
from ferry.adapters.copilot import paths as cp_paths
from ferry.adapters.copilot.writer import (
    INDEX_KEY,
    drop_index_entry,
    index_lists,
    upsert_index_entry,
)
from ferry.adapters.removal import remove, survey
from ferry.cli.flows import run_remove
from ferry.core import Bundle
from ferry.core import provenance as provenance_store
from ferry.core.backup import backup_root
from ferry.core.manifest import Manifest, SourceMachine
from ferry.ucs import Conversation, Message, Provenance, TextBlock, Workspace
from tests.importcontract import BUILDERS
from tests.test_codex_index import SCHEMA
from tests.test_flows import _Answers

PROJECT = "31bdc1d1-6c93-4c25-b66f-b98c6d439809"
IDENTIFIER = b"models/some/resource/name/here"


@pytest.fixture(autouse=True)
def _nothing_is_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """What is open on the machine running the suite must not decide a result.

    Both checks read the real process list, so a developer with VS Code or
    Antigravity open would see these tests refuse to delete anything.
    """
    monkeypatch.setattr("ferry.adapters.copilot.adapter.vs_code_is_running", lambda env=None: False)
    monkeypatch.setattr(
        "ferry.adapters.antigravity.adapter.antigravity_is_running", lambda env=None: False
    )


# --------------------------------------------------------------------------
# four stores to import into
# --------------------------------------------------------------------------


@dataclass
class Target:
    adapter: Adapter
    written: Callable[[], list[Path]]
    """Every conversation file now in the store."""

    listed: Callable[[UUID], bool] | None
    """Whether the tool's own list names a conversation. ``None`` for Claude
    Code, which lists whatever is on disk."""


def claude_code(tmp_path: Path) -> Target:
    root = tmp_path / "claude"
    root.mkdir(parents=True)
    return Target(
        adapter=ClaudeCodeAdapter({cc_paths.CONFIG_DIR_ENV: str(root)}),
        written=lambda: sorted((root / "projects").rglob("*.jsonl")),
        listed=None,
    )


def codex(tmp_path: Path) -> Target:
    home = tmp_path / "codex"
    home.mkdir(parents=True)
    database = home / "state_5.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(SCHEMA)
    connection.commit()
    connection.close()

    def listed(conversation_id: UUID) -> bool:
        connection = sqlite3.connect(database)
        try:
            row = connection.execute(
                "SELECT 1 FROM threads WHERE id = ?", (str(conversation_id),)
            ).fetchone()
        finally:
            connection.close()
        return row is not None

    return Target(
        adapter=CodexAdapter({CODEX_HOME_ENV: str(home)}),
        written=lambda: sorted((home / "sessions").rglob("*.jsonl")),
        listed=listed,
    )


def copilot(tmp_path: Path) -> Target:
    user = tmp_path / "copilot" / "Code" / "User"
    user.mkdir(parents=True)
    return Target(
        adapter=CopilotAdapter({cp_paths.USER_DIR_ENV: str(user)}),
        written=lambda: sorted(user.rglob("*.jsonl")),
        listed=lambda cid: str(cid) in index_lists(user / "globalStorage" / "state.vscdb"),
    )


def _antigravity_ids(index: Path) -> set[UUID]:
    found: set[UUID] = set()
    for field in wire.parse(index.read_bytes()) or []:
        if field.number == 1:
            inner = {f.number: f.value for f in (wire.parse(field.value) or [])}
            found.add(UUID(inner[1].decode()))
    return found


def antigravity(tmp_path: Path) -> Target:
    store = tmp_path / "antigravity" / "data"
    store.mkdir(parents=True)
    index = store / "agyhub_summaries_proto.pb"
    index.write_bytes(b"")
    projects = tmp_path / "antigravity" / "config" / "projects"
    projects.mkdir(parents=True)
    (projects / f"{PROJECT}.json").write_text(
        json.dumps(
            {
                "id": PROJECT,
                "name": "work",
                "projectResources": {
                    "resources": [{"gitFolder": {"folderUri": "file:///c%3A/work"}}]
                },
                "updatedAt": "2026-09-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return Target(
        adapter=AntigravityAdapter({ag_paths.DATA_DIR_ENV: str(store)}),
        written=lambda: sorted((store / "conversations").glob("*.db")),
        listed=lambda cid: cid in _antigravity_ids(index),
    )


TARGETS: dict[str, Callable[[Path], Target]] = {
    "claude-code": claude_code,
    "codex": codex,
    "copilot": copilot,
    "antigravity": antigravity,
}


@pytest.fixture(params=sorted(TARGETS))
def tool(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def target(tool: str, tmp_path: Path) -> Target:
    return TARGETS[tool](tmp_path)


def a_conversation(target_tool: str, title: str = "A migrated conversation") -> Conversation:
    """A conversation from some *other* tool, so importing it is a conversion."""
    return Conversation(
        id=uuid4(),
        source_tool="claude-code" if target_tool == "codex" else "codex",
        title=title,
        workspace=Workspace(original_path="/home/bob/work"),
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        updated_at=datetime(2026, 9, 2, tzinfo=UTC),
        messages=[
            Message(role="user", content=[TextBlock(text="what did you do?")]),
            Message(role="assistant", content=[TextBlock(text="I read a file.")]),
        ],
    )


def import_into(target: Target, tmp_path: Path, *items: Conversation) -> list[Path]:
    root = tmp_path / f"bundle-{uuid4().hex[:8]}"
    bundle = Bundle.create(
        root,
        Manifest(
            bundle_version="1.0",
            created_at=datetime(2026, 9, 1, tzinfo=UTC),
            created_by="ferry",
            source_machine=SourceMachine(hostname="h", os="win32"),
            tools_included=sorted({item.source_tool for item in items}),
            conversation_count=len(items),
        ),
    )
    for item in items:
        bundle.add_conversation(item)
    events = list(target.adapter.import_(root, ImportOptions(allow_cross_tool=True)))
    failed = [event.message for event in events if event.kind == "error"]
    assert not failed, failed
    written = target.written()
    assert len(written) == len(items), "the fixture import did not write what it was given"
    return written


def an_origin(tool: str) -> Provenance:
    return Provenance(
        original_tool="codex",
        imported_into=tool,
        imported_at=datetime(2026, 9, 1, tzinfo=UTC),
        ferry_version="0.1.0",
        lossy=True,
        conversion_notes=[],
    )


# --------------------------------------------------------------------------
# what is offered
# --------------------------------------------------------------------------


class TestWhatIsOffered:
    def test_a_conversation_ferry_converted_is_offered(
        self, tool: str, target: Target, tmp_path: Path
    ) -> None:
        item = a_conversation(tool)
        [path] = import_into(target, tmp_path, item)

        [found] = survey(target.adapter)

        assert found.removable
        assert found.came_from == item.source_tool
        assert found.title == "A migrated conversation", "a list of file names is not choosable"
        assert found.path == path

    @pytest.mark.parametrize("case_name", sorted(BUILDERS))
    def test_a_restore_is_never_offered(self, case_name: str, tmp_path: Path) -> None:
        """A restore puts back the person's own conversation. Ferry keeps no
        record of it, so there is nothing that could claim it was Ferry's."""
        case = BUILDERS[case_name](tmp_path)
        list(case.adapter.import_(case.bundle, ImportOptions()))
        assert case.written(), "the restore wrote nothing, so this proves nothing"

        assert survey(case.adapter) == []

    def test_a_conversation_carried_on_since_stays(
        self, tool: str, target: Target, tmp_path: Path
    ) -> None:
        """The one this whole feature is built around: migrated, then used.
        It holds real work, and Ferry once having created it is no reason to
        delete that."""
        [path] = import_into(target, tmp_path, a_conversation(tool))
        with path.open("ab") as handle:
            handle.write(b"\n")

        [found] = survey(target.adapter)
        events = list(remove(target.adapter, RemoveOptions()))

        assert found.state == "changed"
        assert path.is_file()
        notes = [event.message for event in events if event.kind == "note"]
        assert notes == ["1 conversation left alone: " + found.why_it_stays]

    def test_a_record_with_no_fingerprint_is_never_offered(
        self, tool: str, target: Target, tmp_path: Path
    ) -> None:
        """No fingerprint means Ferry cannot tell whether the person used it,
        and "cannot tell" is never read as "safe"."""
        [path] = import_into(target, tmp_path, a_conversation(tool))
        record_id = survey(target.adapter)[0].record_id
        provenance_store.record(tool, record_id, an_origin(tool))  # no `written`

        [found] = survey(target.adapter)
        list(remove(target.adapter, RemoveOptions()))

        assert found.state == "unknown"
        assert path.is_file()

    def test_a_record_naming_a_file_outside_this_store_is_not_followed(
        self, tool: str, target: Target, tmp_path: Path
    ) -> None:
        """Written while the tool pointed somewhere else. Following it out of
        the store being cleaned is how a test home reaches into a real one."""
        outside = tmp_path / "somebody-elses.jsonl"
        outside.write_text("mine", encoding="utf-8")
        provenance_store.record(
            tool, uuid4(), an_origin(tool), written=provenance_store.fingerprint(outside)
        )

        [found] = survey(target.adapter)
        list(remove(target.adapter, RemoveOptions()))

        assert found.state == "elsewhere"
        assert outside.read_text(encoding="utf-8") == "mine"

    def test_a_path_climbing_out_of_the_store_is_not_inside_it(
        self, tool: str, target: Target, tmp_path: Path
    ) -> None:
        outside = tmp_path / "escaped.jsonl"
        outside.write_text("mine", encoding="utf-8")
        [root] = target.adapter.written_roots()[:1]
        climbing = root / ".." / ".." / ".." / outside.name
        written = provenance_store.fingerprint(outside)
        assert written is not None
        provenance_store.record(
            tool,
            uuid4(),
            an_origin(tool),
            written=provenance_store.Written(str(climbing), written.sha256, written.bytes),
        )

        assert [found.state for found in survey(target.adapter)] == ["elsewhere"]


# --------------------------------------------------------------------------
# deleting
# --------------------------------------------------------------------------


class TestDeleting:
    def test_the_file_its_entry_and_the_record_all_go(
        self, tool: str, target: Target, tmp_path: Path
    ) -> None:
        item = a_conversation(tool)
        import_into(target, tmp_path, item)
        if target.listed is not None:
            assert target.listed(item.id), "the fixture import did not list it"

        events = list(remove(target.adapter, RemoveOptions()))

        assert target.written() == []
        if target.listed is not None:
            assert not target.listed(item.id), "the list still names a conversation that is gone"
        assert provenance_store.recorded(tool) == []
        assert events[-1].message == "1 of 1 deleted"

    def test_only_the_chosen_conversation_goes(
        self, tool: str, target: Target, tmp_path: Path
    ) -> None:
        keep, drop = a_conversation(tool, "keep"), a_conversation(tool, "drop")
        import_into(target, tmp_path, keep, drop)
        chosen = next(found for found in survey(target.adapter) if found.title == "drop")

        list(remove(target.adapter, RemoveOptions(only=frozenset({str(chosen.record_id)}))))

        [left] = survey(target.adapter)
        assert left.title == "keep"
        assert left.removable
        assert len(target.written()) == 1
        if target.listed is not None:
            assert target.listed(keep.id)
            assert not target.listed(drop.id)

    def test_a_preview_deletes_nothing(self, tool: str, target: Target, tmp_path: Path) -> None:
        item = a_conversation(tool)
        import_into(target, tmp_path, item)

        events = list(remove(target.adapter, RemoveOptions(dry_run=True)))

        assert len(target.written()) == 1
        if target.listed is not None:
            assert target.listed(item.id)
        assert provenance_store.recorded(tool)
        [said] = [event.message for event in events if event.kind == "progress"]
        assert said.startswith("would delete")
        assert events[-1].message == "1 of 1 would be deleted"

    def test_a_copy_is_kept_before_anything_is_deleted(
        self, tool: str, target: Target, tmp_path: Path
    ) -> None:
        [path] = import_into(target, tmp_path, a_conversation(tool))

        list(remove(target.adapter, RemoveOptions()))

        copies = {copy.name for copy in backup_root().rglob("*") if copy.is_file()}
        assert path.name in copies

    def test_nothing_is_deleted_while_the_tool_is_open(
        self, tool: str, target: Target, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import_into(target, tmp_path, a_conversation(tool))
        monkeypatch.setattr(target.adapter, "in_use", lambda: "close it first")

        events = list(remove(target.adapter, RemoveOptions()))

        assert len(target.written()) == 1
        assert [event.message for event in events if event.kind == "error"] == ["close it first"]

    def test_an_entry_that_will_not_come_out_stops_the_delete_before_the_file(
        self, tool: str, target: Target, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file still there and still listed is exactly what the person had.
        The record stays too, so running it again finishes the job."""
        import_into(target, tmp_path, a_conversation(tool))
        real = target.adapter.unlist
        locked = {"now": True}

        def unlist(written: Path) -> None:
            if locked["now"]:
                raise RemovalBlocked("the list is locked")
            real(written)

        monkeypatch.setattr(target.adapter, "unlist", unlist)

        events = list(remove(target.adapter, RemoveOptions()))

        assert len(target.written()) == 1
        assert provenance_store.recorded(tool), "the record went, so nothing could finish this"
        assert any("the list is locked" in event.message for event in events)

        locked["now"] = False
        list(remove(target.adapter, RemoveOptions()))
        assert target.written() == []

    def test_running_it_twice_is_harmless(self, tool: str, target: Target, tmp_path: Path) -> None:
        import_into(target, tmp_path, a_conversation(tool))
        list(remove(target.adapter, RemoveOptions()))

        events = list(remove(target.adapter, RemoveOptions()))

        assert events[-1].message.startswith("nothing to delete")


class TestWhatGoesWithTheFile:
    def test_spilled_tool_output_goes_with_a_claude_code_transcript(self, tmp_path: Path) -> None:
        target = claude_code(tmp_path)
        [transcript] = import_into(target, tmp_path, a_conversation("claude-code"))
        spilled = transcript.parent / transcript.stem / SIDECAR_SUBDIR
        spilled.mkdir(parents=True)
        (spilled / "one.txt").write_text("output", encoding="utf-8")

        list(remove(target.adapter, RemoveOptions()))

        assert not (transcript.parent / transcript.stem).exists()
        assert transcript.parent.is_dir(), "the project folder is Claude Code's, not Ferry's"

    def test_anything_else_in_that_folder_is_left(self, tmp_path: Path) -> None:
        target = claude_code(tmp_path)
        [transcript] = import_into(target, tmp_path, a_conversation("claude-code"))
        other = transcript.parent / transcript.stem / "subagents" / "a.jsonl"
        other.parent.mkdir(parents=True)
        other.write_text("{}", encoding="utf-8")

        list(remove(target.adapter, RemoveOptions()))

        assert other.is_file()

    def test_work_waiting_in_an_antigravity_journal_counts_as_use(self, tmp_path: Path) -> None:
        """The database can be byte for byte what Ferry wrote while the
        conversation carries on in ``-wal`` beside it."""
        target = antigravity(tmp_path)
        [database] = import_into(target, tmp_path, a_conversation("antigravity"))
        database.with_name(database.name + "-wal").write_bytes(b"x" * 32)

        [found] = survey(target.adapter)
        list(remove(target.adapter, RemoveOptions()))

        assert found.state == "changed"
        assert database.is_file()

    def test_an_empty_journal_goes_with_the_database(self, tmp_path: Path) -> None:
        target = antigravity(tmp_path)
        [database] = import_into(target, tmp_path, a_conversation("antigravity"))
        journal = database.with_name(database.name + "-wal")
        shared = database.with_name(database.name + "-shm")
        journal.write_bytes(b"")
        shared.write_bytes(b"\0" * 16)

        list(remove(target.adapter, RemoveOptions()))

        assert not database.exists()
        assert not journal.exists()
        assert not shared.exists()

    def test_opening_an_antigravity_conversation_to_look_is_not_using_it(
        self, tmp_path: Path
    ) -> None:
        """Found by the human: a conversation imported, opened once to check
        it had arrived, and then refused by the delete as "changed". Rebuilt
        and compared on their real store, six header bytes had moved - SQLite
        switching the file to WAL as Antigravity opened it - and not one byte of
        the conversation. This does the same thing with SQLite itself."""
        target = antigravity(tmp_path)
        [database] = import_into(target, tmp_path, a_conversation("antigravity"))
        record_id = survey(target.adapter)[0].record_id
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.close()
        assert provenance_store.untouched_since_import("antigravity", record_id) is False, (
            "opening it did not change the file, so this proves nothing"
        )

        [found] = survey(target.adapter)
        list(remove(target.adapter, RemoveOptions()))

        assert found.removable
        assert not database.exists()

    def test_a_step_taken_out_after_opening_still_counts_as_use(self, tmp_path: Path) -> None:
        """The proof has to fail for any real change, or it is not a proof."""
        target = antigravity(tmp_path)
        [database] = import_into(target, tmp_path, a_conversation("antigravity"))
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA journal_mode=WAL")
        with connection:
            connection.execute("DELETE FROM steps WHERE rowid = (SELECT MAX(rowid) FROM steps)")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()

        [found] = survey(target.adapter)
        list(remove(target.adapter, RemoveOptions()))

        assert found.state == "changed"
        assert database.is_file()


class TestOpenedNotChanged:
    def test_a_file_of_another_size_is_never_passed(self, tmp_path: Path) -> None:
        path = tmp_path / "a.db"
        path.write_bytes(b"SQLite format 3\x00" + b"\0" * 200)
        was = provenance_store.fingerprint(path)
        assert was is not None
        path.write_bytes(path.read_bytes() + b"\0")

        assert opened_not_changed(path, was) is False

    def test_a_file_that_is_not_sqlite_is_never_passed(self, tmp_path: Path) -> None:
        path = tmp_path / "a.db"
        path.write_bytes(b"not a database" + b"\0" * 200)
        was = provenance_store.fingerprint(path)
        assert was is not None
        path.write_bytes(b"not a databasf" + b"\0" * 200)

        assert opened_not_changed(path, was) is False

    def test_a_change_past_the_header_is_never_passed(self, tmp_path: Path) -> None:
        path = tmp_path / "a.db"
        path.write_bytes(b"SQLite format 3\x00" + b"\0" * 200)
        was = provenance_store.fingerprint(path)
        assert was is not None
        data = bytearray(path.read_bytes())
        data[150] = 1
        path.write_bytes(bytes(data))

        assert opened_not_changed(path, was) is False


# --------------------------------------------------------------------------
# taking one entry out of each tool's list
# --------------------------------------------------------------------------


def _codex_row(conversation_id: UUID) -> dict[str, object]:
    return thread_row(
        conversation_id=conversation_id,
        rollout_path=Path("/store/rollout.jsonl"),
        cwd="/work/project",
        title="t",
        first_message="m",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        updated_at=datetime(2026, 9, 2, tzinfo=UTC),
        cli_version="ferry-0.1.0",
    )


def _copilot_entry(session_id: str) -> dict[str, object]:
    return {"sessionId": session_id, "title": session_id, "timing": {"created": 1}}


def _antigravity_entry(conversation_id: UUID) -> bytes:
    return entry_for(
        conversation_id,
        title="t",
        project_id=PROJECT,
        identifier=IDENTIFIER,
        steps=2,
        created=1788000000,
        updated=1788000100,
    )


class TestTakingOneEntryOut:
    def test_codex_loses_one_row_and_keeps_the_rest(self, tmp_path: Path) -> None:
        database = tmp_path / "state_5.sqlite"
        connection = sqlite3.connect(database)
        connection.execute(SCHEMA)
        connection.commit()
        connection.close()
        mine, theirs = uuid4(), uuid4()
        upsert_thread_row(database, _codex_row(mine))
        upsert_thread_row(database, _codex_row(theirs))

        assert drop_thread_row(database, mine) is True
        assert drop_thread_row(database, mine) is False

        connection = sqlite3.connect(database)
        remaining = {row[0] for row in connection.execute("SELECT id FROM threads")}
        connection.close()
        assert remaining == {str(theirs)}

    def test_codex_with_no_database_has_nothing_to_take_out(self, tmp_path: Path) -> None:
        assert drop_thread_row(tmp_path / "absent.sqlite", uuid4()) is False

    def test_copilot_keeps_every_other_conversation(self, tmp_path: Path) -> None:
        database = tmp_path / "state.vscdb"
        upsert_index_entry(database, _copilot_entry("mine"))
        upsert_index_entry(database, _copilot_entry("theirs"))

        assert drop_index_entry(database, "mine") is True
        assert drop_index_entry(database, "mine") is False
        assert index_lists(database) == {"theirs"}

    def test_copilot_leaves_a_list_it_cannot_read_exactly_as_it_is(self, tmp_path: Path) -> None:
        database = tmp_path / "state.vscdb"
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)"
        )
        connection.execute("INSERT INTO ItemTable VALUES (?, ?)", (INDEX_KEY, "{ not json"))
        connection.commit()
        connection.close()

        assert drop_index_entry(database, "mine") is False

        connection = sqlite3.connect(database)
        [value] = connection.execute("SELECT value FROM ItemTable").fetchone()
        connection.close()
        assert value == "{ not json"

    def test_antigravity_keeps_every_other_entry_byte_for_byte(self, tmp_path: Path) -> None:
        mine, theirs = uuid4(), uuid4()
        path = tmp_path / "agyhub_summaries_proto.pb"
        path.write_bytes(_antigravity_entry(theirs) + _antigravity_entry(mine))

        assert drop_entry(path, mine) is True
        assert path.read_bytes() == _antigravity_entry(theirs)

    def test_antigravity_with_nothing_to_take_out_is_not_rewritten(self, tmp_path: Path) -> None:
        path = tmp_path / "agyhub_summaries_proto.pb"
        path.write_bytes(_antigravity_entry(uuid4()))
        before = path.read_bytes()

        assert drop_entry(path, uuid4()) is False
        assert path.read_bytes() == before
        assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]

    def test_antigravity_running_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "agyhub_summaries_proto.pb"
        path.write_bytes(b"")

        with pytest.raises(AntigravityIndexLocked, match="running"):
            drop_entry(path, uuid4(), running=True)

    def test_antigravity_with_no_index_has_nothing_to_take_out(self, tmp_path: Path) -> None:
        assert drop_entry(tmp_path / "absent.pb", uuid4()) is False


# --------------------------------------------------------------------------
# the records
# --------------------------------------------------------------------------


class TestTheRecords:
    def test_every_record_is_listed_and_a_stray_file_is_not(self, tmp_path: Path) -> None:
        env = {provenance_store.ROOT_ENV: str(tmp_path)}
        first = uuid4()
        provenance_store.record("codex", first, an_origin("codex"), env)
        (tmp_path / "codex" / "notes.json").write_text("{}", encoding="utf-8")

        assert provenance_store.recorded("codex", env) == [first]

    def test_the_title_is_kept_beside_the_record(self, tmp_path: Path) -> None:
        env = {provenance_store.ROOT_ENV: str(tmp_path)}
        titled, untitled = uuid4(), uuid4()
        provenance_store.record("codex", titled, an_origin("codex"), env, title="A title")
        provenance_store.record("codex", untitled, an_origin("codex"), env)

        assert provenance_store.title_of("codex", titled, env) == "A title"
        assert provenance_store.title_of("codex", untitled, env) is None


# --------------------------------------------------------------------------
# the screen
# --------------------------------------------------------------------------


def installed(*adapters: Adapter) -> list[tuple[Adapter, DetectResult]]:
    return [(adapter, DetectResult(installed=True)) for adapter in adapters]


class TestTheScreen:
    def test_with_nothing_imported_it_says_so_and_asks_nothing(self, tmp_path: Path) -> None:
        target = codex(tmp_path)
        ui = _Answers()

        run_remove(ui, installed(target.adapter))

        assert "nothing here Ferry imported" in ui.text
        assert ui.ticked_from == []
        assert ui.asked_to_confirm == 0

    def test_nothing_is_ticked_to_begin_with(self, tmp_path: Path) -> None:
        target = codex(tmp_path)
        import_into(target, tmp_path, a_conversation("codex"))
        ui = _Answers(confirm=False)

        run_remove(ui, installed(target.adapter))

        assert ui.preselected == [[]]

    def test_saying_no_deletes_nothing(self, tmp_path: Path) -> None:
        target = codex(tmp_path)
        import_into(target, tmp_path, a_conversation("codex"))
        ui = _Answers(confirm=False)

        run_remove(ui, installed(target.adapter))

        assert len(target.written()) == 1
        assert "Nothing was deleted." in ui.text

    def test_saying_yes_deletes_what_was_ticked(self, tmp_path: Path) -> None:
        target = codex(tmp_path)
        import_into(target, tmp_path, a_conversation("codex"))
        ui = _Answers(confirm=True)

        run_remove(ui, installed(target.adapter))

        assert target.written() == []
        assert "1 of 1 deleted" in ui.text

    def test_ticking_nothing_asks_nothing_more(self, tmp_path: Path) -> None:
        target = codex(tmp_path)
        import_into(target, tmp_path, a_conversation("codex"))
        ui = _Answers(ticks=[[]])

        run_remove(ui, installed(target.adapter))

        assert ui.asked_to_confirm == 0
        assert len(target.written()) == 1

    def test_an_open_tool_is_named_before_anything_is_chosen(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = codex(tmp_path)
        import_into(target, tmp_path, a_conversation("codex"))
        monkeypatch.setattr(target.adapter, "in_use", lambda: "Codex is open.")
        ui = _Answers()

        run_remove(ui, installed(target.adapter))

        assert "Codex is open." in ui.text
        assert ui.ticked_from == []
        assert len(target.written()) == 1

    def test_a_conversation_carried_on_is_shown_as_staying_and_not_offered(
        self, tmp_path: Path
    ) -> None:
        target = codex(tmp_path)
        import_into(
            target, tmp_path, a_conversation("codex", "used"), a_conversation("codex", "untouched")
        )
        used = next(found for found in survey(target.adapter) if found.title == "used")
        assert used.path is not None
        with used.path.open("ab") as handle:
            handle.write(b"\n")
        untouched = next(found for found in survey(target.adapter) if found.title == "untouched")
        ui = _Answers(confirm=False)

        run_remove(ui, installed(target.adapter))

        assert ui.ticked_from == [[str(untouched.record_id)]]
        assert "will stay" in ui.text

    def test_with_two_tools_it_asks_which(self, tmp_path: Path) -> None:
        first, second = codex(tmp_path / "a"), claude_code(tmp_path / "b")
        import_into(first, tmp_path / "a", a_conversation("codex"))
        import_into(second, tmp_path / "b", a_conversation("claude-code"))
        ui = _Answers(actions=[second.adapter.name], confirm=True)

        run_remove(ui, installed(first.adapter, second.adapter))

        assert "Delete from which assistant?" in ui.questions
        assert second.written() == []
        assert len(first.written()) == 1
