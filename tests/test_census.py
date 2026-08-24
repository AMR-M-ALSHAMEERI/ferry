"""Counting conversations the way the user counts them.

The rule this module exists to enforce: **the number Ferry shows must be the
number the application shows.** It was not, in all four adapters, and the gap
grew with use -- 18 Copilot session files for 5 conversations, 6 Antigravity
databases for 2.
"""

from __future__ import annotations

import json
from pathlib import Path

from ferry.adapters.census import Census, census, jsonl_holds


def has_messages(_identifier: str, path: Path) -> bool:
    return path.read_text(encoding="utf-8").strip() != ""


class TestCounting:
    def test_counts_files_that_hold_something(self, tmp_path: Path) -> None:
        items = []
        for name in ("a", "b"):
            path = tmp_path / f"{name}.txt"
            path.write_text("hello", encoding="utf-8")
            items.append((name, path))
        assert census(items, has_messages).conversations == 2

    def test_empty_files_are_not_conversations(self, tmp_path: Path) -> None:
        full = tmp_path / "a.txt"
        full.write_text("hello", encoding="utf-8")
        blank = tmp_path / "b.txt"
        blank.write_text("", encoding="utf-8")
        counted = census([("a", full), ("b", blank)], has_messages)
        assert counted.conversations == 1
        assert counted.empty == 1
        assert counted.files == 2

    def test_one_conversation_stored_twice_is_one_conversation(self, tmp_path: Path) -> None:
        """A Copilot conversation open in two workspaces is written to both."""
        first = tmp_path / "one.txt"
        first.write_text("hello", encoding="utf-8")
        second = tmp_path / "two.txt"
        second.write_text("hello", encoding="utf-8")
        counted = census([("same-id", first), ("same-id", second)], has_messages)
        assert counted.conversations == 1
        assert counted.duplicates == 1

    def test_hidden_files_are_neither_conversations_nor_empty(self, tmp_path: Path) -> None:
        """An Antigravity subagent has messages and is still not a conversation.

        Counting it as empty would be as wrong as counting it as a
        conversation, so it has its own tally.
        """
        path = tmp_path / "sub.txt"
        path.write_text("hello", encoding="utf-8")
        counted = census(
            [("sub", path)], has_messages, hidden=lambda _i, _p: True, hidden_label="subagents"
        )
        assert counted.conversations == 0
        assert counted.hidden == 1
        assert counted.empty == 0

    def test_hidden_is_checked_before_emptiness(self, tmp_path: Path) -> None:
        path = tmp_path / "sub.txt"
        path.write_text("", encoding="utf-8")
        counted = census([("sub", path)], has_messages, hidden=lambda _i, _p: True)
        assert counted.hidden == 1
        assert counted.empty == 0

    def test_nothing_at_all(self) -> None:
        assert census([], has_messages) == Census()


class TestNotes:
    def test_silent_when_the_count_matches_the_files(self, tmp_path: Path) -> None:
        """ "18 files, 18 conversations" tells the user nothing."""
        path = tmp_path / "a.txt"
        path.write_text("hello", encoding="utf-8")
        assert census([("a", path)], has_messages).notes() == []

    def test_explains_each_kind_of_gap(self) -> None:
        counted = Census(files=18, conversations=5, empty=12, duplicates=1)
        notes = " ".join(counted.notes())
        assert "12 empty" in notes
        assert "1 duplicate copy" in notes

    def test_plural_reads_as_english(self) -> None:
        assert "2 duplicate copies" in " ".join(Census(duplicates=2).notes())
        assert "1 duplicate copy" in " ".join(Census(duplicates=1).notes())


class TestJsonlHolds:
    def _write(self, path: Path, records: list[dict]) -> Path:
        path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
        )
        return path

    def test_finds_a_matching_record(self, tmp_path: Path) -> None:
        path = self._write(tmp_path / "a.jsonl", [{"type": "meta"}, {"type": "user"}])
        assert jsonl_holds(path, lambda r: r.get("type") == "user")

    def test_reports_absence(self, tmp_path: Path) -> None:
        path = self._write(tmp_path / "a.jsonl", [{"type": "meta"}])
        assert not jsonl_holds(path, lambda r: r.get("type") == "user")

    def test_stops_at_the_first_match(self, tmp_path: Path) -> None:
        """What makes this affordable at startup: real transcripts are megabytes."""
        path = self._write(tmp_path / "a.jsonl", [{"type": "user"}] * 500)
        seen = 0

        def test(record: dict) -> bool:
            nonlocal seen
            seen += 1
            return record.get("type") == "user"

        assert jsonl_holds(path, test)
        assert seen == 1

    def test_a_broken_line_does_not_condemn_the_file(self, tmp_path: Path) -> None:
        """A transcript being written while Ferry reads it can end mid-line."""
        path = tmp_path / "a.jsonl"
        path.write_text('{"type": "meta"}\n{"type": "us\n', encoding="utf-8")
        assert not jsonl_holds(path, lambda r: r.get("type") == "user")

    def test_a_file_that_cannot_be_read_counts_as_a_conversation(self, tmp_path: Path) -> None:
        """Unreadable is not empty.

        Hiding it from the count would mean the user never learns it exists.
        Counting it lets the export be the thing that reports why it failed.
        """
        assert jsonl_holds(tmp_path / "missing.jsonl", lambda _r: False)

    def test_blank_lines_are_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "a.jsonl"
        path.write_text('\n\n{"type": "user"}\n', encoding="utf-8")
        assert jsonl_holds(path, lambda r: r.get("type") == "user")
