"""Noticing when a format has changed - and staying quiet when it has not.

The behaviour these pin down is the one that decides whether the warning is
worth anything. These applications update constantly and almost never change
how they store conversations, so a warning tied to the **version number**
starts firing weeks after release and never stops. By the time the format
really does change, it is furniture.

So: silent on a format that still matches, whatever the version says. Specific
about what broke when it does not.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ferry.adapters.antigravity import paths as ag_paths
from ferry.adapters.antigravity import schema
from ferry.adapters.antigravity.adapter import AntigravityAdapter
from ferry.adapters.antigravity.adapter import check_format as antigravity_format
from ferry.adapters.claude_code.adapter import check_format as claude_code_format
from ferry.adapters.codex.adapter import check_format as codex_format
from ferry.adapters.copilot.adapter import check_format as copilot_format
from ferry.adapters.formatcheck import FormatCheck
from tests.antigravity_fixture import build_database, message, string_field

CONV = "aaaaaaaa-1111-4111-8111-111111111111"


class TestCaveats:
    def test_a_matching_format_says_nothing(self) -> None:
        """Even when the version is one nobody has ever tested against.

        This is the whole design. Tying the warning to the version number
        means warning forever after the first update that changes nothing.
        """
        assert FormatCheck(checked=True).caveats("Antigravity", "99.0.0") == []

    def test_a_changed_format_says_what_changed(self) -> None:
        checked = FormatCheck(checked=True, findings=["no message text was found"])
        caveat = checked.caveats("Antigravity", "2.9.0")[0]
        assert "2.9.0" in caveat
        assert "no message text was found" in caveat

    def test_nothing_to_read_is_not_reported_as_a_pass(self) -> None:
        """A question that could not be asked is not a question answered."""
        caveat = FormatCheck(checked=False).caveats("Codex", "1.0")[0]
        assert "could not check" in caveat

    def test_the_caveat_does_not_repeat_the_tool_name_the_screen_prints(self) -> None:
        caveat = FormatCheck(checked=False).caveats("Antigravity", "2.8.1")[0]
        assert not caveat.startswith("Antigravity")


class TestAntigravity:
    def test_a_normal_conversation_passes(self, tmp_path: Path) -> None:
        database = build_database(
            tmp_path / f"{CONV}.db",
            [(0, schema.USER_INPUT, "hello"), (1, schema.PLANNER_RESPONSE, "hi")],
        )
        assert antigravity_format(database).ok

    def test_a_renumbered_text_field_is_caught(self, tmp_path: Path) -> None:
        """The failure this format is prone to, and the only silent one.

        Move the assistant's text to a different field number and every
        conversation exports with its turns present and empty. Nothing errors,
        nothing crashes, and the export reports complete success.
        """
        database = build_database(
            tmp_path / f"{CONV}.db",
            [(index, schema.PLANNER_RESPONSE, "an answer") for index in range(6)],
        )
        connection = sqlite3.connect(database)
        # Field 20 becomes field 23 - a plausible renumbering.
        connection.execute(
            "UPDATE steps SET step_payload = ?", (message(23, string_field(1, "an answer")),)
        )
        connection.commit()
        connection.close()

        checked = antigravity_format(database)
        assert not checked.ok
        assert "no message text was found" in checked.findings[0]

    def test_unreadable_blobs_are_caught(self, tmp_path: Path) -> None:
        database = build_database(tmp_path / f"{CONV}.db", [(0, schema.USER_INPUT, "hello")])
        connection = sqlite3.connect(database)
        connection.execute("UPDATE steps SET step_payload = ?", (b"\x0b\x0cnot protobuf",))
        connection.commit()
        connection.close()

        checked = antigravity_format(database)
        assert not checked.ok
        assert "not readable protobuf" in checked.findings[0]

    def test_an_empty_database_is_not_a_failure(self, tmp_path: Path) -> None:
        database = build_database(tmp_path / f"{CONV}.db", [])
        assert not antigravity_format(database).checked

    def test_a_missing_file_is_not_a_failure(self, tmp_path: Path) -> None:
        assert not antigravity_format(tmp_path / "absent.db").checked

    def test_the_adapter_stays_silent_on_a_healthy_store(self, tmp_path: Path) -> None:
        build_database(
            tmp_path / "antigravity" / "conversations" / f"{CONV}.db",
            [(0, schema.USER_INPUT, "hello")],
        )
        env = {ag_paths.DATA_DIR_ENV: str(tmp_path / "antigravity")}
        assert AntigravityAdapter(env).detect().caveats == []


def _jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


class TestClaudeCode:
    def test_normal_transcript_passes(self, tmp_path: Path) -> None:
        path = _jsonl(
            tmp_path / "a.jsonl",
            [{"type": "user", "message": {"content": "hi"}} for _ in range(4)],
        )
        assert claude_code_format(path).ok

    def test_turns_with_no_content_are_caught(self, tmp_path: Path) -> None:
        path = _jsonl(tmp_path / "a.jsonl", [{"type": "user", "text": "hi"} for _ in range(4)])
        checked = claude_code_format(path)
        assert not checked.ok
        assert "no message content" in checked.findings[0]

    def test_a_transcript_with_no_turns_is_not_a_failure(self, tmp_path: Path) -> None:
        path = _jsonl(tmp_path / "a.jsonl", [{"type": "summary"}])
        assert not claude_code_format(path).checked


class TestCodex:
    def test_normal_rollout_passes(self, tmp_path: Path) -> None:
        path = _jsonl(
            tmp_path / "a.jsonl",
            [{"payload": {"role": "user", "content": "hi"}} for _ in range(4)],
        )
        assert codex_format(path).ok

    def test_turns_with_no_content_are_caught(self, tmp_path: Path) -> None:
        path = _jsonl(tmp_path / "a.jsonl", [{"payload": {"role": "user"}} for _ in range(4)])
        checked = codex_format(path)
        assert not checked.ok
        assert "no content" in checked.findings[0]

    def test_metadata_only_is_not_a_failure(self, tmp_path: Path) -> None:
        path = _jsonl(tmp_path / "a.jsonl", [{"type": "session_meta", "payload": {"id": "x"}}])
        assert not codex_format(path).checked


class TestCopilot:
    def _session(self, path: Path, turns: list[dict]) -> Path:
        path.write_text(json.dumps({"kind": 0, "v": {"requests": turns}}) + "\n", encoding="utf-8")
        return path

    def test_normal_transcript_passes(self, tmp_path: Path) -> None:
        path = self._session(
            tmp_path / "a.jsonl", [{"message": {"text": "hi"}, "response": []} for _ in range(3)]
        )
        assert copilot_format(path).ok

    def test_turns_missing_both_halves_are_caught(self, tmp_path: Path) -> None:
        path = self._session(tmp_path / "a.jsonl", [{"prompt": "hi"} for _ in range(3)])
        checked = copilot_format(path)
        assert not checked.ok
        assert "neither a question nor an answer" in checked.findings[0]

    def test_an_empty_chat_is_not_a_failure(self, tmp_path: Path) -> None:
        """VS Code writes one of these whenever a chat panel opens."""
        assert not copilot_format(self._session(tmp_path / "a.jsonl", [])).checked
