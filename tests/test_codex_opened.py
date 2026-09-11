"""Opening a Codex conversation is not using it, even though Codex rewrites it.

Found by the human: a conversation imported into Codex, opened, and then refused
by the delete as "changed". Rebuilt and compared on their real store, Codex had
rewritten every line into its current format - an ``ordinal`` on each, display
events turned into ``item_completed``, two fields added to ``session_meta`` -
and left every timestamp and every ``response_item`` exactly as Ferry wrote it.

``migrate`` below does what was measured. Everything else must still count as
use.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ferry.adapters.base import RemoveOptions
from ferry.adapters.codex.opened import content_fingerprint
from ferry.adapters.removal import remove, survey
from ferry.core import provenance as provenance_store
from tests.test_removal import a_conversation, codex, import_into

DISPLAY_EVENTS = ("agent_message", "agent_reasoning", "user_message")


def records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def migrate(path: Path) -> None:
    """What Codex 0.147 did to a rollout Ferry wrote, on opening it."""
    rows = []
    for ordinal, record in enumerate(records(path)):
        payload = record.get("payload")
        if record.get("type") == "session_meta" and isinstance(payload, dict):
            payload = {**payload, "base_instructions": {"text": ""}, "history_mode": "legacy"}
        if record.get("type") == "event_msg" and isinstance(payload, dict):
            if payload.get("type") in DISPLAY_EVENTS:
                payload = {"type": "item_completed", "item": {"from": payload["type"]}}
        rows.append({"ordinal": ordinal, **record, "payload": payload})
    write(path, rows)


def record_id(target: Any) -> Any:
    return survey(target.adapter)[0].record_id


def test_an_import_records_the_second_checksum(tmp_path: Path) -> None:
    target = codex(tmp_path)
    [path] = import_into(target, tmp_path, a_conversation("codex"))

    written = provenance_store.written_file("codex", record_id(target))

    assert written is not None
    assert written.content == content_fingerprint(path)


def test_the_rollout_really_does_have_display_events_to_rewrite(tmp_path: Path) -> None:
    """Otherwise the migration below changes nothing and proves nothing."""
    target = codex(tmp_path)
    [path] = import_into(target, tmp_path, a_conversation("codex"))

    kinds = {row["payload"].get("type") for row in records(path) if row.get("type") == "event_msg"}

    assert kinds & set(DISPLAY_EVENTS)


def test_codex_rewriting_it_on_opening_is_not_using_it(tmp_path: Path) -> None:
    target = codex(tmp_path)
    item = a_conversation("codex")
    [path] = import_into(target, tmp_path, item)
    migrate(path)
    assert provenance_store.untouched_since_import("codex", record_id(target)) is False

    [found] = survey(target.adapter)
    list(remove(target.adapter, RemoveOptions()))

    assert found.removable
    assert target.written() == []
    assert target.listed is not None
    assert not target.listed(item.id)


def test_a_new_turn_after_opening_still_counts_as_use(tmp_path: Path) -> None:
    target = codex(tmp_path)
    [path] = import_into(target, tmp_path, a_conversation("codex"))
    migrate(path)
    rows = records(path)
    rows.append(
        {
            "timestamp": "2026-09-11T12:00:00.000Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": []},
        }
    )
    write(path, rows)

    [found] = survey(target.adapter)

    assert found.state == "changed"


def test_a_new_display_event_after_opening_still_counts_as_use(tmp_path: Path) -> None:
    target = codex(tmp_path)
    [path] = import_into(target, tmp_path, a_conversation("codex"))
    migrate(path)
    rows = records(path)
    rows.append(
        {
            "timestamp": "2026-09-11T12:00:00.000Z",
            "type": "event_msg",
            "payload": {"type": "task_started"},
        }
    )
    write(path, rows)

    [found] = survey(target.adapter)

    assert found.state == "changed"


def test_a_changed_message_still_counts_as_use(tmp_path: Path) -> None:
    target = codex(tmp_path)
    [path] = import_into(target, tmp_path, a_conversation("codex"))
    migrate(path)
    rows = records(path)
    model = next(row for row in rows if row.get("type") == "response_item")
    model["payload"] = {**model["payload"], "edited": True}
    write(path, rows)

    [found] = survey(target.adapter)

    assert found.state == "changed"


def test_a_record_from_before_the_second_checksum_has_nothing_to_prove_against(
    tmp_path: Path,
) -> None:
    target = codex(tmp_path)
    [path] = import_into(target, tmp_path, a_conversation("codex"))
    identifier = record_id(target)
    written = provenance_store.written_file("codex", identifier)
    origin = provenance_store.recall("codex", identifier)
    assert written is not None and origin is not None
    provenance_store.record(
        "codex",
        identifier,
        origin,
        written=provenance_store.Written(written.path, written.sha256, written.bytes),
    )
    migrate(path)

    [found] = survey(target.adapter)

    assert found.state == "changed"


def test_the_second_checksum_survives_being_written_and_read(tmp_path: Path) -> None:
    env = {provenance_store.ROOT_ENV: str(tmp_path)}
    target = codex(tmp_path / "store")
    import_into(target, tmp_path / "store", a_conversation("codex"))
    identifier = record_id(target)
    origin = provenance_store.recall("codex", identifier)
    assert origin is not None
    kept = provenance_store.Written("p", "s", 1, content="c")

    provenance_store.record("codex", identifier, origin, env, written=kept)

    assert provenance_store.written_file("codex", identifier, env) == kept
