"""Writing back into VS Code's chat storage.

The index is the part worth guarding. It is one JSON blob holding every
conversation the user has, so a careless write does not fail loudly -- it
deletes their chat list while appearing to add to it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from ferry.adapters.copilot.writer import (
    INDEX_KEY,
    index_entry,
    index_lists,
    recorded_workspace_key,
    snapshot_line,
    upsert_index_entry,
)
from ferry.ucs import Conversation, Message, TextBlock, Workspace

CONV = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")


def conversation(**extra) -> Conversation:  # type: ignore[no-untyped-def]
    fields = {
        "id": CONV,
        "source_tool": "copilot",
        "title": "A chat",
        "created_at": datetime(2026, 7, 21, tzinfo=UTC),
        "updated_at": datetime(2026, 7, 22, tzinfo=UTC),
        "workspace": Workspace(),
        "messages": [Message(role="user", content=[TextBlock(text="hello")])],
        "source_raw": {
            "document": {"sessionId": "something-else", "creationDate": 1784647795094},
            "workspace_key": "abc123",
        },
    }
    fields.update(extra)
    return Conversation(**fields)  # type: ignore[arg-type]


def test_the_transcript_is_one_snapshot_record() -> None:
    """A finished conversation has no edits left to replay, and one snapshot is
    what replaying the deltas would arrive at anyway."""
    record = json.loads(snapshot_line(conversation()))

    assert record["kind"] == 0
    assert "v" in record


def test_the_internal_id_is_forced_to_match_the_filename() -> None:
    """They agreed on every real file. A transcript whose id disagrees with its
    name is the kind of thing that works until suddenly it does not."""
    record = json.loads(snapshot_line(conversation()))

    assert record["v"]["sessionId"] == str(CONV)


def test_a_conversation_with_no_original_document_gets_one_built() -> None:
    """Was a refusal until M7b.2 Phase 2, and is now the whole feature.

    The refusal was right while Ferry could only replay a document it had
    read. Once VS Code was measured accepting a built one (#203, #204), the
    same input has an answer instead of an error.
    """
    document = json.loads(snapshot_line(conversation(source_raw=None)))["v"]

    assert document["version"] == 3
    assert document["sessionId"] == str(CONV)
    # Never dressed as Copilot's own work: the rule that a converted
    # conversation is not presented as native.
    assert document["responderUsername"] != "GitHub Copilot"


def test_the_index_entry_carries_what_vs_code_sorts_by() -> None:
    entry = index_entry(conversation())

    assert entry["sessionId"] == str(CONV)
    assert entry["title"] == "A chat"
    assert entry["timing"]["created"] == 1784647795094
    assert entry["isEmpty"] is False


def test_an_untitled_conversation_still_gets_a_name() -> None:
    entry = index_entry(conversation(title=None))

    assert entry["title"] == "New Chat"


def test_the_workspace_it_came_from_is_recoverable() -> None:
    assert recorded_workspace_key(conversation()) == "abc123"


# --------------------------------------------------------------------------
# the index
# --------------------------------------------------------------------------


def test_writing_an_entry_creates_the_database_if_needed(tmp_path: Path) -> None:
    """A workspace that has never held a chat has no state.vscdb, and that is
    a normal destination rather than an error."""
    database = tmp_path / "fresh" / "state.vscdb"

    upsert_index_entry(database, index_entry(conversation()))

    assert index_lists(database) == {str(CONV)}


def test_writing_an_entry_keeps_the_ones_already_there(tmp_path: Path) -> None:
    database = tmp_path / "state.vscdb"
    upsert_index_entry(database, {"sessionId": "first"})

    upsert_index_entry(database, index_entry(conversation()))

    assert index_lists(database) == {"first", str(CONV)}


def test_writing_the_same_entry_twice_replaces_it(tmp_path: Path) -> None:
    database = tmp_path / "state.vscdb"
    upsert_index_entry(database, index_entry(conversation()))

    upsert_index_entry(database, index_entry(conversation(title="Renamed")))

    connection = sqlite3.connect(database)
    stored = json.loads(
        connection.execute("SELECT value FROM ItemTable WHERE key = ?", (INDEX_KEY,)).fetchone()[0]
    )
    connection.close()
    assert stored["entries"][str(CONV)]["title"] == "Renamed"


def test_a_table_without_vs_codes_conflict_clause_still_accepts_a_write(tmp_path: Path) -> None:
    """The write says `OR REPLACE` itself rather than relying on a detail of
    someone else's schema, which a future VS Code is free to change."""
    database = tmp_path / "state.vscdb"
    connection = sqlite3.connect(database)
    with connection:
        connection.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)")
        connection.execute("INSERT INTO ItemTable VALUES (?, ?)", (INDEX_KEY, "{}"))
    connection.close()

    upsert_index_entry(database, index_entry(conversation()))

    assert index_lists(database) == {str(CONV)}


def test_an_unreadable_index_is_replaced_rather_than_crashing(tmp_path: Path) -> None:
    database = tmp_path / "state.vscdb"
    connection = sqlite3.connect(database)
    with connection:
        connection.execute(
            "CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)"
        )
        connection.execute("INSERT INTO ItemTable VALUES (?, ?)", (INDEX_KEY, "not json"))
    connection.close()

    upsert_index_entry(database, index_entry(conversation()))

    assert index_lists(database) == {str(CONV)}


def test_listing_a_database_that_is_not_there_is_empty_not_an_error(tmp_path: Path) -> None:
    assert index_lists(tmp_path / "absent.vscdb") == set()
