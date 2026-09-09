"""Writing an Antigravity conversation, which was refused until it was measured.

Two stores have to agree before a person sees anything: the database holds the
conversation, and an entry in ``agyhub_summaries_proto.pb`` is what makes
Antigravity know it exists. A database with no entry is invisible -- proved by
cloning a conversation Antigravity *does* list, changing only its ids, and
watching it not appear.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from ferry.adapters.antigravity import paths as ag_paths
from ferry.adapters.antigravity import reader, wire
from ferry.adapters.antigravity.adapter import AntigravityAdapter
from ferry.adapters.antigravity.build import (
    PLANNER_RESPONSE,
    USER_INPUT,
    build_database,
    said_by,
    trajectory_blob,
)
from ferry.adapters.antigravity.index import (
    AntigravityIndexLocked,
    entry_for,
    upsert_entry,
)
from ferry.adapters.base import ImportOptions
from ferry.core import Bundle
from ferry.core import provenance as provenance_store
from ferry.core.manifest import Manifest, SourceMachine
from ferry.ucs import (
    Conversation,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Workspace,
)

IDENTIFIER = b"models/some/resource/name/here"
PROJECT = "31bdc1d1-6c93-4c25-b66f-b98c6d439809"

#: Both root conversations on the probe machine carried exactly these.
ROOT_FIELDS = [1, 2, 3, 6, 7, 10, 18]

#: A real index entry's summary, measured across all six.
SUMMARY_FIELDS = [1, 2, 3, 4, 5, 7, 9, 10, 15, 16, 17, 22]


def a_conversation(**kwargs: object) -> Conversation:
    defaults: dict[str, object] = {
        "id": uuid4(),
        "source_tool": "claude-code",
        "workspace": Workspace(original_path="/home/bob/work"),
        "title": "A migrated conversation",
        "created_at": datetime(2026, 9, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 2, tzinfo=UTC),
        "messages": [
            Message(role="user", content=[TextBlock(text="what did you do?")]),
            Message(role="assistant", content=[TextBlock(text="I read a file.")]),
        ],
    }
    defaults.update(kwargs)
    return Conversation(**defaults)  # type: ignore[arg-type]


def fields_of(blob: bytes) -> dict[int, bytes]:
    return {f.number: f.value for f in (wire.parse(blob) or [])}


class TestTheConversationCanBeReadBack:
    def test_ferry_reads_what_ferry_wrote(self, tmp_path: Path) -> None:
        item = a_conversation()
        path = tmp_path / f"{item.id}.db"

        steps = build_database(path, item, project_id=PROJECT, identifier=IDENTIFIER)

        assert steps == 2
        read = reader.read_conversation(path).conversation
        assert read is not None
        assert [m.role for m in read.messages] == ["user", "assistant"]
        said = [b.text for m in read.messages for b in m.content if isinstance(b, TextBlock)]
        assert said == ["what did you do?", "I read a file."]

    def test_the_question_is_written_where_the_interface_draws_it(self, tmp_path: Path) -> None:
        """A real USER_INPUT step carries the same bytes at 19.2 **and** 19.3.1.

        Writing only 19.2 produced a conversation whose title bar held the
        question and whose bubble was empty. One is what is sent to the model,
        the other is what is drawn -- the third tool to keep two copies of a
        turn, after Copilot's ``parts`` and Codex's ``event_msg``.
        """
        item = a_conversation()
        path = tmp_path / f"{item.id}.db"
        build_database(path, item, project_id=PROJECT, identifier=IDENTIFIER)

        connection = sqlite3.connect(path)
        payload = connection.execute(
            "SELECT step_payload FROM steps WHERE step_type = ?", (USER_INPUT,)
        ).fetchone()[0]
        connection.close()

        found = dict(wire.strings(payload))
        assert found.get((19, 2)) == "what did you do?"
        assert found.get((19, 3, 1)) == "what did you do?", "the bubble would be empty"


class TestWhatIsNotWritten:
    def test_a_tool_call_becomes_text_not_a_step_antigravity_could_have_run(
        self, tmp_path: Path
    ) -> None:
        """CODE_ACTION is Antigravity doing something. It did not do this."""
        item = a_conversation(
            messages=[
                Message(role="user", content=[TextBlock(text="read it")]),
                Message(
                    role="assistant",
                    content=[
                        TextBlock(text="Reading."),
                        ToolUseBlock(id="c1", name="Read", input={"file_path": "a.py"}),
                        ToolResultBlock(tool_use_id="c1", output="print(1)"),
                    ],
                ),
            ]
        )
        path = tmp_path / f"{item.id}.db"
        build_database(path, item, project_id=PROJECT, identifier=IDENTIFIER)

        connection = sqlite3.connect(path)
        types = [row[0] for row in connection.execute("SELECT step_type FROM steps")]
        connection.close()
        assert set(types) <= {USER_INPUT, PLANNER_RESPONSE}
        assert 5 not in types, "a CODE_ACTION step would claim Antigravity ran it"

    def test_a_tool_result_that_is_not_a_string_is_still_written(self) -> None:
        """`ToolResultBlock.output` is `Any`, and Codex records a list of blocks.

        A real import of six Codex conversations failed with
        `'list' object has no attribute 'strip'` -- 0 of 6 written, because this
        assumed a string. Every adapter that has met this settled it the same
        way, and now they share the one flattener.
        """
        message = Message(
            role="assistant",
            content=[
                ToolUseBlock(id="c1", name="Read", input={"file_path": "a.py"}),
                ToolResultBlock(
                    tool_use_id="c1",
                    output=[{"type": "text", "text": "first"}, {"type": "text", "text": "second"}],
                ),
            ],
        )

        said = said_by(message, "codex")

        assert "first" in said
        assert "second" in said

    def test_thinking_is_dropped_rather_than_written_unsigned(self) -> None:
        message = Message(
            role="assistant",
            content=[
                ThinkingBlock(text="considering the options", signature="from-another-vendor"),
                TextBlock(text="Here is the answer."),
            ],
        )

        said = said_by(message, "claude-code")

        assert said == "Here is the answer."
        assert "considering" not in said

    def test_a_built_conversation_is_never_a_subagent(self) -> None:
        """Field 5 names a parent, and Antigravity never lists a conversation
        that has one. Two rounds of this milestone were lost to a template
        borrowed from a subagent."""
        blob = trajectory_blob(uuid4(), PROJECT, 1788000000, IDENTIFIER)

        assert 5 not in fields_of(blob)
        assert sorted(fields_of(blob)) == ROOT_FIELDS

    def test_the_undecoded_field_is_left_out_rather_than_invented(self) -> None:
        """Field 15 is 352-380 bytes that do not parse and differ per
        conversation. A conversation without it opens and reads."""
        assert 15 not in fields_of(trajectory_blob(uuid4(), PROJECT, 1788000000, IDENTIFIER))


class TestTheSecondStore:
    def an_index(self, tmp_path: Path, *entries: bytes) -> Path:
        path = tmp_path / "agyhub_summaries_proto.pb"
        path.write_bytes(b"".join(entries))
        return path

    def an_entry(self, conversation_id: UUID, title: str = "A migrated conversation") -> bytes:
        return entry_for(
            conversation_id,
            title=title,
            project_id=PROJECT,
            identifier=IDENTIFIER,
            steps=2,
            created=1788000000,
            updated=1788000100,
        )

    def test_an_entry_has_the_shape_a_real_one_has(self) -> None:
        entry = self.an_entry(uuid4())

        inner = fields_of(wire.parse(entry)[0].value)
        assert sorted(fields_of(inner[2])) == SUMMARY_FIELDS

    def test_the_step_count_is_what_the_summary_carries(self) -> None:
        """Field 2 matched the database exactly in all six real entries."""
        entry = entry_for(
            uuid4(),
            title="t",
            project_id=PROJECT,
            identifier=IDENTIFIER,
            steps=17,
            created=1,
            updated=2,
        )

        summary = fields_of(fields_of(wire.parse(entry)[0].value)[2])
        assert summary[2] == bytes([17])

    def test_a_conversation_is_added_to_the_list(self, tmp_path: Path) -> None:
        path = self.an_index(tmp_path)
        conversation_id = uuid4()

        upsert_entry(path, conversation_id, self.an_entry(conversation_id))

        entries = [f for f in wire.parse(path.read_bytes()) or [] if f.number == 1]
        assert len(entries) == 1
        assert fields_of(entries[0].value)[1].decode() == str(conversation_id)

    def test_importing_twice_leaves_one_entry(self, tmp_path: Path) -> None:
        conversation_id = uuid4()
        path = self.an_index(tmp_path, self.an_entry(conversation_id))

        upsert_entry(path, conversation_id, self.an_entry(conversation_id, "renamed"))

        entries = [f for f in wire.parse(path.read_bytes()) or [] if f.number == 1]
        assert len(entries) == 1
        summary = fields_of(fields_of(entries[0].value)[2])
        assert summary[1].decode() == "renamed"

    def test_everyone_elses_conversations_survive(self, tmp_path: Path) -> None:
        """This file is the list of somebody's conversations. Losing a
        neighbour here loses their history, not a row."""
        theirs, mine = uuid4(), uuid4()
        path = self.an_index(tmp_path, self.an_entry(theirs, "not mine"))

        upsert_entry(path, mine, self.an_entry(mine))

        entries = [f for f in wire.parse(path.read_bytes()) or [] if f.number == 1]
        assert len(entries) == 2
        assert {fields_of(e.value)[1].decode() for e in entries} == {str(theirs), str(mine)}

    def test_a_running_antigravity_is_refused_rather_than_written_underneath(
        self, tmp_path: Path
    ) -> None:
        """It holds this file in memory and writes it back on exit, so a write
        underneath it is discarded -- and reporting success would be a lie the
        person discovers when the list is unchanged."""
        conversation_id = uuid4()
        path = self.an_index(tmp_path)

        with pytest.raises(AntigravityIndexLocked, match="running"):
            upsert_entry(path, conversation_id, self.an_entry(conversation_id), running=True)

    def test_a_missing_index_is_refused_rather_than_created(self, tmp_path: Path) -> None:
        conversation_id = uuid4()

        with pytest.raises(AntigravityIndexLocked):
            upsert_entry(tmp_path / "absent.pb", conversation_id, self.an_entry(conversation_id))


class TestTheRecordOfAConversion:
    """A converted conversation that looked native would be indistinguishable,
    months later, from one that really happened in Antigravity."""

    def a_store(self, tmp_path: Path) -> dict[str, str]:
        store = tmp_path / "store"
        store.mkdir(parents=True)
        (store / "agyhub_summaries_proto.pb").write_bytes(b"")
        projects = tmp_path / "config" / "projects"
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
        return {ag_paths.DATA_DIR_ENV: str(store)}

    def a_bundle(self, tmp_path: Path, item: Conversation) -> Path:
        root = tmp_path / "bundle"
        Bundle.create(
            root,
            Manifest(
                bundle_version="1.0",
                created_at=datetime(2026, 9, 1, tzinfo=UTC),
                created_by="ferry",
                source_machine=SourceMachine(hostname="h", os="win32"),
                tools_included=["codex"],
                conversation_count=1,
            ),
        ).add_conversation(item)
        return root

    def test_the_origin_survives_being_exported_again(self, tmp_path: Path) -> None:
        """Exporting reads the record back, or the origin stops at this machine.

        A built conversation looks native on the way out - that is why the
        record lives in Ferry's own store and not in Antigravity's file. The
        other three adapters have always recalled it on export; this one did
        not, which made the promise in CROSS-TOOL.md untrue of Antigravity.
        Found by sweeping all four for a defect discovered in one.
        """
        item = a_conversation(source_tool="codex")
        env = self.a_store(tmp_path)
        adapter = AntigravityAdapter(env)

        list(adapter.import_(self.a_bundle(tmp_path, item), ImportOptions(allow_cross_tool=True)))
        list(adapter.export(tmp_path / "out"))

        exported = Bundle.open(tmp_path / "out").load_conversation(item.id)
        assert exported.provenance is not None, "the export forgot where it came from"
        assert exported.provenance.original_tool == "codex"

    def test_a_built_conversation_says_where_it_came_from(self, tmp_path: Path) -> None:
        """The field was set on an object and dropped once before (A7b.8), and
        recorded without the costs once after (#220). Both halves, both times."""
        item = a_conversation(
            source_tool="codex",
            messages=[
                Message(role="user", content=[TextBlock(text="read it")]),
                Message(
                    role="assistant",
                    content=[
                        TextBlock(text="Reading."),
                        ToolUseBlock(id="c1", name="shell", input={"command": ["ls"]}),
                        ToolResultBlock(tool_use_id="c1", output=[{"text": "a.py"}]),
                    ],
                ),
            ],
        )
        env = self.a_store(tmp_path)

        events = list(
            AntigravityAdapter(env).import_(
                self.a_bundle(tmp_path, item), ImportOptions(allow_cross_tool=True)
            )
        )

        assert sum(1 for e in events if e.kind == "progress") == 1
        recorded = provenance_store.recall("antigravity", item.id)
        assert recorded is not None, "a conversion left no record of itself"
        assert recorded.original_tool == "codex"
        assert recorded.imported_into == "antigravity"
        assert recorded.lossy is True
        notes = " | ".join(recorded.conversion_notes)
        assert "tool call" in notes, f"the cost the screen counted is unrecorded: {notes}"
        assert "never as steps Antigravity is shown as having run" in notes
