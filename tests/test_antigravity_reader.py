"""Turning a conversation database into UCS."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ferry.adapters.antigravity import reader, schema
from ferry.adapters.antigravity.paths import DATA_DIR_ENV
from ferry.ucs import ImageBlock, TextBlock, ToolUseBlock
from tests.antigravity_fixture import build_database

CONV = "aaaaaaaa-1111-4111-8111-111111111111"
PROJECT = "99999999-9999-4999-8999-999999999999"


def make(
    tmp_path: Path, steps: list[tuple[int, int, str]], **kwargs
) -> tuple[Path, dict[str, str]]:
    database = build_database(
        tmp_path / "antigravity" / "conversations" / f"{CONV}.db", steps, **kwargs
    )
    return database, {DATA_DIR_ENV: str(tmp_path / "antigravity")}


class TestMessages:
    def test_user_text_becomes_a_user_message(self, tmp_path: Path) -> None:
        database, env = make(tmp_path, [(0, schema.USER_INPUT, "please refactor this")])
        found = reader.read_conversation(database, env)
        assert found.conversation is not None
        message = found.conversation.messages[0]
        assert message.role == "user"
        assert isinstance(message.content[0], TextBlock)
        assert message.content[0].text == "please refactor this"

    def test_planner_text_becomes_an_assistant_message(self, tmp_path: Path) -> None:
        database, env = make(tmp_path, [(0, schema.PLANNER_RESPONSE, "here is the plan")])
        found = reader.read_conversation(database, env)
        assert found.conversation is not None
        assert found.conversation.messages[0].role == "assistant"

    def test_a_tool_step_becomes_a_tool_use_named_after_its_type(self, tmp_path: Path) -> None:
        database, env = make(tmp_path, [(0, 21, "ls -la")])
        found = reader.read_conversation(database, env)
        assert found.conversation is not None
        block = found.conversation.messages[0].content[0]
        assert isinstance(block, ToolUseBlock)
        assert block.name == "RUN_COMMAND"
        assert block.input["detail"] == "ls -la"

    def test_steps_keep_their_order(self, tmp_path: Path) -> None:
        database, env = make(
            tmp_path,
            [(0, schema.USER_INPUT, "first"), (1, schema.PLANNER_RESPONSE, "second")],
        )
        found = reader.read_conversation(database, env)
        assert found.conversation is not None
        assert [m.role for m in found.conversation.messages] == ["user", "assistant"]

    def test_a_step_with_no_text_is_left_out(self, tmp_path: Path) -> None:
        database, env = make(
            tmp_path, [(0, schema.USER_INPUT, "kept"), (1, schema.USER_INPUT, "   ")]
        )
        found = reader.read_conversation(database, env)
        assert found.conversation is not None
        assert len(found.conversation.messages) == 1


class TestCheckpointsAndUnknowns:
    def test_checkpoints_are_skipped_and_reported(self, tmp_path: Path) -> None:
        """They hold file snapshots, not conversation, and are the bulk of the bytes.

        Nothing is lost by leaving them out: the original database goes into
        the bundle whole, and that is what an import restores from.
        """
        database, env = make(
            tmp_path,
            [(0, schema.USER_INPUT, "hi"), (1, schema.CHECKPOINT, "a file snapshot")],
        )
        found = reader.read_conversation(database, env)
        assert found.conversation is not None
        assert len(found.conversation.messages) == 1
        # Counted, not narrated. The adapter phrases it once for the whole
        # export rather than once per conversation.
        assert found.checkpoints == 1

    def test_an_unnamed_step_type_warns_rather_than_being_guessed_at(self, tmp_path: Path) -> None:
        """Type 28 is real, occurs 12 times, and appears in no transcript."""
        database, env = make(tmp_path, [(0, 28, "something")])
        found = reader.read_conversation(database, env)
        assert found.unknown_types == (28,)

    def test_an_unnamed_step_is_never_attributed_to_the_user(self, tmp_path: Path) -> None:
        assert schema.role_of(28) != "user"


class TestTimes:
    def test_created_and_updated_come_from_the_data(self, tmp_path: Path) -> None:
        database, env = make(
            tmp_path,
            [(0, schema.USER_INPUT, "a"), (5, schema.PLANNER_RESPONSE, "b")],
            created=1_785_000_000,
        )
        found = reader.read_conversation(database, env)
        assert found.conversation is not None
        assert found.conversation.created_at == datetime.fromtimestamp(1_785_000_000, UTC)
        assert found.conversation.updated_at == datetime.fromtimestamp(1_785_000_005, UTC)

    def test_an_implausible_timestamp_is_refused(self, tmp_path: Path) -> None:
        """A varint read at the wrong path is still a valid integer.

        Trusting it dates the conversation thousands of years out, which sorts
        to the top of every list the user sees.
        """
        database, env = make(tmp_path, [(0, schema.USER_INPUT, "a")], created=99_999_999_999)
        found = reader.read_conversation(database, env)
        assert found.conversation is not None
        assert found.conversation.created_at.year < 2200


class TestWorkspace:
    def test_the_project_folder_is_decoded_from_its_uri(self, tmp_path: Path) -> None:
        projects = tmp_path / "config" / "projects"
        projects.mkdir(parents=True)
        (projects / f"{PROJECT}.json").write_text(
            json.dumps(
                {
                    "id": PROJECT,
                    "name": "Work",
                    "projectResources": {
                        "resources": [{"folderUri": "file:///c%3A%5CUsers%5CDell%5CWork"}]
                    },
                }
            ),
            encoding="utf-8",
        )
        database, env = make(tmp_path, [(0, schema.USER_INPUT, "a")])
        found = reader.read_conversation(database, env)
        assert found.conversation is not None
        assert found.conversation.workspace.original_path == r"C:\Users\Dell\Work"
        assert found.conversation.workspace.name == "Work"
        assert found.project_id == PROJECT

    def test_an_unknown_project_is_not_invented(self, tmp_path: Path) -> None:
        database, env = make(tmp_path, [(0, schema.USER_INPUT, "a")])
        found = reader.read_conversation(database, env)
        assert found.conversation is not None
        assert found.conversation.workspace.original_path is None


class TestAttachments:
    def test_uploads_are_listed_and_placed(self, tmp_path: Path) -> None:
        directory = tmp_path / "antigravity" / "brain" / CONV / ".user_uploaded"
        directory.mkdir(parents=True)
        (directory / "media_1786163647832.png").write_bytes(b"\x89PNG fake")
        database, env = make(tmp_path, [(0, schema.USER_INPUT, "look at this")])
        found = reader.read_conversation(database, env)
        assert found.conversation is not None
        assert len(found.conversation.attachments) == 1
        assert isinstance(found.conversation.messages[-1].content[0], ImageBlock)

    def test_attachment_ids_are_stable_across_reads(self, tmp_path: Path) -> None:
        """Two exports of unchanged history must produce the same bundle.

        Random ids made them impossible to compare, which was a real defect in
        the Copilot adapter before it was caught.
        """
        directory = tmp_path / "antigravity" / "brain" / CONV / ".user_uploaded"
        directory.mkdir(parents=True)
        (directory / "media_1.png").write_bytes(b"x")
        database, env = make(tmp_path, [(0, schema.USER_INPUT, "a")])
        first = reader.read_conversation(database, env).conversation
        second = reader.read_conversation(database, env).conversation
        assert first is not None and second is not None
        assert first.attachments[0].id == second.attachments[0].id


class TestFailure:
    def test_a_file_that_is_not_a_database_is_reported_not_raised(self, tmp_path: Path) -> None:
        directory = tmp_path / "antigravity" / "conversations"
        directory.mkdir(parents=True)
        broken = directory / f"{CONV}.db"
        broken.write_bytes(b"not a database")
        found = reader.read_conversation(broken, {DATA_DIR_ENV: str(tmp_path / "antigravity")})
        assert found.conversation is None
        assert found.warnings

    def test_a_filename_that_is_not_an_id_is_reported(self, tmp_path: Path) -> None:
        found = reader.read_conversation(tmp_path / "notes.db")
        assert found.conversation is None
        assert "not named after a conversation id" in found.warnings[0]


class TestTitle:
    def test_the_title_is_the_first_thing_the_user_typed(self, tmp_path: Path) -> None:
        database, env = make(
            tmp_path,
            [(0, schema.PLANNER_RESPONSE, "assistant first"), (1, schema.USER_INPUT, "my ask")],
        )
        found = reader.read_conversation(database, env)
        assert found.conversation is not None
        assert found.conversation.title == "my ask"

    def test_a_long_first_message_is_trimmed(self, tmp_path: Path) -> None:
        database, env = make(tmp_path, [(0, schema.USER_INPUT, "x" * 500)])
        found = reader.read_conversation(database, env)
        assert found.conversation is not None
        assert found.conversation.title is not None
        assert len(found.conversation.title) <= 80
        assert found.conversation.title.endswith("...")


class TestSubagentTrajectories:
    """A subagent gets its own database and is not a conversation the user has.

    Antigravity spawns subagents, gives each one a database in
    ``conversations/`` that looks exactly like a conversation, and never lists
    it. On the reference machine that is 6 databases for the 2 conversations
    the app shows. Counting files would have reported three times as many
    conversations as exist.
    """

    def test_a_top_level_conversation_has_no_parent(self, tmp_path: Path) -> None:
        database, env = make(tmp_path, [(0, schema.USER_INPUT, "a")])
        assert reader.parent_conversation(database, env) is None

    def test_a_subagent_names_its_parent(self, tmp_path: Path) -> None:
        parent = "bbbbbbbb-2222-4222-8222-222222222222"
        database, env = make(tmp_path, [(0, schema.USER_INPUT, "do the task")], parent=parent)
        assert reader.parent_conversation(database, env) == parent

    def test_the_parent_is_recorded_on_the_conversation(self, tmp_path: Path) -> None:
        parent = "bbbbbbbb-2222-4222-8222-222222222222"
        database, env = make(tmp_path, [(0, schema.USER_INPUT, "do the task")], parent=parent)
        found = reader.read_conversation(database, env)
        assert found.parent_id == parent
        assert found.conversation is not None
        assert found.conversation.source_raw is not None
        assert found.conversation.source_raw["parent_conversation"] == parent

    def test_a_conversation_naming_itself_is_top_level(self, tmp_path: Path) -> None:
        """Field 6 holds the conversation's own id at the top of the tree."""
        database, env = make(tmp_path, [(0, schema.USER_INPUT, "a")], parent=CONV)
        assert reader.parent_conversation(database, env) is None

    def test_disagreeing_fields_are_treated_as_top_level(self, tmp_path: Path) -> None:
        """Showing a subagent is a smaller harm than hiding a real conversation.

        Fields 5 and 6 say the same thing from opposite directions. If a
        future version breaks that, Ferry must fail towards showing the user
        too much rather than silently dropping something they wrote.
        """
        from tests.antigravity_fixture import message, string_field, varint_field

        database, env = make(tmp_path, [(0, schema.USER_INPUT, "a")])
        connection = sqlite3.connect(database)
        connection.execute(
            "UPDATE trajectory_metadata_blob SET data = ?",
            (
                message(2, varint_field(1, 1_785_000_000))
                + string_field(5, "cccccccc-3333-4333-8333-333333333333")
                + string_field(6, "dddddddd-4444-4444-8444-444444444444"),
            ),
        )
        connection.commit()
        connection.close()
        assert reader.parent_conversation(database, env) is None
