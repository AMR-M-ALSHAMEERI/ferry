"""A conversation written into Codex is a conversation Codex offers you.

Ferry wrote the rollout and no index row, so a migrated conversation could be
opened only by `codex resume <id>` -- an id nobody migrating their history
knows. The picker, which is how people actually find a session, showed nothing.

**The third time this project has met the same gate.** A Copilot transcript
missing from the chat index, a Claude Code conversation in an untrusted folder,
and now this: the file intact, the words all present, the tool's list empty.
Every target has something after the write, and finding it is part of the work.

Phase 3 was verified by id and I called Codex supported on that evidence, which
was the claim the evidence did not cover.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from ferry.adapters.codex.index import (
    APPROVAL_MODE,
    COLUMNS,
    SANDBOX_POLICY,
    ThreadIndexLocked,
    thread_row,
    upsert_thread_row,
)

#: The real schema, narrowed to what Ferry writes. Kept here rather than
#: imported from the module under test: a fixture built from the same tuple the
#: code writes would agree with it by construction and assert nothing.
SCHEMA = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    source TEXT NOT NULL,
    model_provider TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT NOT NULL,
    sandbox_policy TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    has_user_event INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    archived_at INTEGER,
    cli_version TEXT NOT NULL DEFAULT '',
    first_user_message TEXT NOT NULL DEFAULT '',
    memory_mode TEXT NOT NULL DEFAULT 'enabled',
    model TEXT,
    reasoning_effort TEXT,
    created_at_ms INTEGER,
    updated_at_ms INTEGER,
    thread_source TEXT,
    preview TEXT NOT NULL DEFAULT '',
    recency_at INTEGER NOT NULL DEFAULT 0,
    recency_at_ms INTEGER NOT NULL DEFAULT 0,
    history_mode TEXT NOT NULL DEFAULT 'legacy',
    is_pinned INTEGER NOT NULL DEFAULT 0
)
"""


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "state_5.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(SCHEMA)
    connection.commit()
    connection.close()
    return path


def a_row(conversation_id: UUID | None = None) -> dict[str, object]:
    return thread_row(
        conversation_id=conversation_id or uuid4(),
        rollout_path=Path("/store/rollout.jsonl"),
        cwd="/work/project",
        title="a migrated conversation",
        first_message="what did you do?",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        updated_at=datetime(2026, 9, 2, tzinfo=UTC),
        cli_version="ferry-0.1.0",
    )


class TestTheRowLandsAndIsFindable:
    def test_a_written_conversation_appears_in_the_index(self, database: Path) -> None:
        conversation_id = uuid4()

        upsert_thread_row(database, a_row(conversation_id))

        connection = sqlite3.connect(database)
        found = connection.execute(
            "SELECT title, cwd, rollout_path FROM threads WHERE id = ?", (str(conversation_id),)
        ).fetchone()
        connection.close()
        assert found is not None, "the conversation is on disk and nothing lists it"
        assert found[0] == "a migrated conversation"
        assert found[1] == "/work/project"

    def test_importing_the_same_conversation_twice_leaves_one_row(self, database: Path) -> None:
        """Ferry derives ids from the conversation, so a re-import is the same
        conversation arriving again -- not a second copy of it."""
        conversation_id = uuid4()

        upsert_thread_row(database, a_row(conversation_id))
        upsert_thread_row(database, a_row(conversation_id))

        connection = sqlite3.connect(database)
        count = connection.execute(
            "SELECT COUNT(*) FROM threads WHERE id = ?", (str(conversation_id),)
        ).fetchone()[0]
        connection.close()
        assert count == 1

    def test_a_row_never_takes_the_neighbours_with_it(self, database: Path) -> None:
        """The Copilot index is one JSON blob holding every conversation, where
        a careless write deletes a person's history while appearing to add to
        it. This is a table of rows and cannot do that -- asserted rather than
        assumed, because the consequence is somebody's history."""
        mine, theirs = uuid4(), uuid4()
        upsert_thread_row(database, a_row(theirs))

        upsert_thread_row(database, a_row(mine))

        connection = sqlite3.connect(database)
        count = connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        connection.close()
        assert count == 2


class TestTheSafetyChoices:
    """Two values here decide what a session may do, and both are chosen.

    They are not formatting, and a later edit that "matches what Codex writes"
    more closely would be handing away the person's approval on their behalf.
    """

    def test_a_migrated_session_still_asks_before_acting(self) -> None:
        assert APPROVAL_MODE == "on-request"
        assert a_row()["approval_mode"] != "never"

    def test_a_migrated_session_is_given_no_permissions(self) -> None:
        """A transcript is a record of work already done. It needs nothing."""
        policy = json.loads(SANDBOX_POLICY)

        assert policy["network"] == "restricted"
        writable = [
            entry
            for entry in policy["file_system"]["entries"]
            if entry.get("access") not in (None, "read")
        ]
        assert not writable, f"a migrated transcript was given write access: {writable}"

    def test_no_machine_specific_claim_is_invented(self) -> None:
        """`model`, `reasoning_effort` and the git columns describe a session
        that ran on this machine. The conversation ran somewhere else."""
        assert "model" not in COLUMNS
        assert "reasoning_effort" not in COLUMNS
        assert not [name for name in COLUMNS if name.startswith("git_")]


class TestWhenItCannotBeWritten:
    def test_a_missing_database_is_refused_rather_than_created(self, tmp_path: Path) -> None:
        """Creating it would mean inventing Codex's schema for an installation
        Ferry does not recognise, and then writing into what it invented."""
        with pytest.raises(ThreadIndexLocked):
            upsert_thread_row(tmp_path / "absent.sqlite", a_row())

    def test_a_file_that_is_not_a_database_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "state_5.sqlite"
        path.write_text("this is not a database", encoding="utf-8")

        with pytest.raises(ThreadIndexLocked):
            upsert_thread_row(path, a_row())
