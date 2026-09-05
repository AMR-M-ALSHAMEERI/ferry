"""Reading, writing and round-tripping Codex rollouts.

Fixtures come from ``tests/fixtures/codex`` — real record shapes from a census
of 25,884 records, invented content. Import assertions read the written file
back with :mod:`json` rather than with Ferry's own reader, so a matched pair of
bugs cannot cancel out.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from ferry.adapters.base import ImportOptions
from ferry.adapters.codex import CodexAdapter
from ferry.adapters.codex.paths import (
    codex_home,
    rollout_files,
    rollout_name,
    thread_id_of,
)
from ferry.adapters.codex.reader import parent_thread, read_rollout
from ferry.adapters.codex.writer import (
    SYNTHETIC_HEADER_FIELDS,
    Rebuild,
    rollout_stamp,
    session_meta_for,
)
from ferry.core import Bundle
from ferry.core import provenance as provenance_store
from ferry.ucs import Conversation, Message, TextBlock, Workspace

FIXTURES = Path(__file__).parent / "fixtures" / "codex"
BASIC_ID = UUID("019f1111-1111-7111-8111-111111111111")
EDGE_ID = UUID("019f2222-2222-7222-8222-222222222222")
EMPTY_ID = UUID("019f3333-3333-7333-8333-333333333333")
PARENT_ID = "019f0000-0000-7000-8000-000000000000"
BACKSLASH = chr(92)


def install(home: Path, fixture: str, thread_id: UUID, day: str) -> Path:
    """Lay a fixture out the way Codex would have: sessions/YYYY/MM/DD/."""
    year, month, date = day.split("-")
    directory = home / "sessions" / year / month / date
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / rollout_name(thread_id, f"{day}T09-00-00")
    destination.write_bytes((FIXTURES / fixture).read_bytes())
    return destination


@pytest.fixture
def source(tmp_path: Path) -> Path:
    home = tmp_path / "source"
    install(home, "basic.jsonl", BASIC_ID, "2026-08-01")
    install(home, "edge.jsonl", EDGE_ID, "2026-08-02")
    install(home, "empty.jsonl", EMPTY_ID, "2026-08-03")
    return home


@pytest.fixture
def adapter(source: Path) -> CodexAdapter:
    return CodexAdapter(env={"CODEX_HOME": str(source)})


@pytest.fixture
def target(tmp_path: Path) -> CodexAdapter:
    return CodexAdapter(env={"CODEX_HOME": str(tmp_path / "target")})


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def basic(source: Path):
    return read_rollout(next((source / "sessions").rglob(f"*{BASIC_ID}.jsonl")))


def edge(source: Path):
    return read_rollout(next((source / "sessions").rglob(f"*{EDGE_ID}.jsonl")))


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------


def test_codex_home_follows_the_environment(tmp_path: Path) -> None:
    assert codex_home({"CODEX_HOME": str(tmp_path)}) == tmp_path
    assert codex_home({}) == Path.home() / ".codex"


def test_only_real_rollout_names_are_picked_up(source: Path) -> None:
    stray = source / "sessions" / "2026" / "08" / "01" / "notes.jsonl"
    stray.write_text("{}", encoding="utf-8")
    other = source / "sessions" / "2026" / "08" / "01" / "rollout-nope.jsonl"
    other.write_text("{}", encoding="utf-8")

    found = rollout_files({"CODEX_HOME": str(source)})
    assert sorted(thread_id_of(p) for p in found) == sorted([BASIC_ID, EDGE_ID, EMPTY_ID])


def test_a_name_that_is_not_a_rollout_has_no_thread_id(tmp_path: Path) -> None:
    assert thread_id_of(tmp_path / "notes.jsonl") is None
    assert thread_id_of(tmp_path / "rollout-2026-08-01T09-00-00-nothex.jsonl") is None


# --------------------------------------------------------------------------
# reading: the two traps
# --------------------------------------------------------------------------


def test_thinking_comes_from_the_event_stream_not_the_encrypted_record(source: Path) -> None:
    """The single most destructive thing this adapter could get wrong.

    ``response_item``/``reasoning`` holds ``encrypted_content`` and no
    plaintext. Mapping it would produce thinking blocks full of ciphertext with
    a block count that looks entirely correct.
    """
    found = basic(source)
    assert found.conversation is not None
    thinking = [b for m in found.conversation.messages for b in m.content if b.type == "thinking"]

    assert len(thinking) == 1
    assert thinking[0].text == "The module is imported in two places."
    # And the ciphertext reached nothing.
    dumped = found.conversation.model_dump_json()
    assert "gAAAAABmZW5jcnlwdGVk" not in dumped
    assert any("encrypted" in note for note in found.notes)


def test_the_event_echo_does_not_double_the_conversation(source: Path) -> None:
    """Every turn appears twice in the file. Reading both doubles it."""
    found = basic(source)
    assert found.conversation is not None

    texts = [b.text for m in found.conversation.messages for b in m.content if b.type == "text"]
    assert texts == ["Rename the gadget module.", "Two files reference it."]
    assert texts.count("Two files reference it.") == 1


def test_every_block_type_survives_with_its_payload(source: Path) -> None:
    found = basic(source)
    assert found.conversation is not None
    kinds = [(m.role, [b.type for b in m.content]) for m in found.conversation.messages]

    assert kinds == [
        ("user", ["text"]),
        ("assistant", ["thinking"]),
        ("assistant", ["tool_use"]),
        ("tool", ["tool_result"]),
        ("assistant", ["text"]),
    ]

    call = found.conversation.messages[2].content[0]
    assert call.name == "shell"
    assert call.input == {"command": "grep -r gadget src"}
    assert call.id == "call_alpha"

    result = found.conversation.messages[3].content[0]
    assert result.tool_use_id == "call_alpha"
    assert result.output == "src/gadget.py\nsrc/app.py"


def test_tool_arguments_arrive_as_a_json_string_and_are_parsed(source: Path) -> None:
    """Codex stores `arguments` as a string; leaving it that way loses structure."""
    found = basic(source)
    assert found.conversation is not None
    assert isinstance(found.conversation.messages[2].content[0].input, dict)


def test_the_session_header_is_kept_so_a_rebuild_can_be_valid(source: Path) -> None:
    found = basic(source)
    assert found.conversation is not None
    header = (found.conversation.source_raw or {}).get("session_meta")

    assert isinstance(header, dict)
    # The two fields Codex rejects the whole file over.
    assert isinstance(header["base_instructions"], dict)
    assert isinstance(header["context_window"], dict)


def test_the_title_is_what_the_person_typed(source: Path) -> None:
    """Found by reading a migrated conversation, not by any automated check.

    A Codex session opens with several screens of injected context -- permissions
    preambles, environment blocks, plugin listings -- recorded as ordinary user
    messages. Titling from the first text block produced
    "<permissions instructions>" for every real conversation on the probe
    machine. Only ``event_msg``/``user_message`` marks a genuinely typed turn.
    """
    found = basic(source)
    assert found.conversation is not None
    assert found.conversation.title == "Rename the gadget module."


def test_the_conversation_is_dated_by_its_messages(source: Path) -> None:
    """Not by the last token-count event, which a rebuild does not reproduce."""
    found = basic(source)
    assert found.conversation is not None

    assert found.conversation.created_at == datetime(2026, 8, 1, 9, 0, 2, tzinfo=UTC)
    assert found.conversation.updated_at == datetime(2026, 8, 1, 9, 0, 6, tzinfo=UTC)


# --------------------------------------------------------------------------
# reading: MCP de-duplication
# --------------------------------------------------------------------------


def test_an_mcp_result_already_in_the_canonical_record_is_dropped(source: Path) -> None:
    found = edge(source)
    assert found.conversation is not None
    outputs = [b for m in found.conversation.messages for b in m.content if b.type == "tool_result"]

    assert [b.tool_use_id for b in outputs] == ["call_dup", "call_only_here"]
    duplicate = [b for b in outputs if b.tool_use_id == "call_dup"]
    assert len(duplicate) == 1
    # The canonical output survived, not the event echo.
    assert duplicate[0].output == [{"type": "text", "text": "canonical output"}]
    assert any("de-duplicated" in note for note in found.notes)


def test_an_mcp_result_that_exists_nowhere_else_is_kept(source: Path) -> None:
    """255 of 461 real MCP results had no canonical counterpart."""
    found = edge(source)
    assert found.conversation is not None
    only_here = [
        b
        for m in found.conversation.messages
        for b in m.content
        if b.type == "tool_result" and b.tool_use_id == "call_only_here"
    ]

    assert len(only_here) == 1
    assert only_here[0].output == {"ok": True, "summary": "exists nowhere else"}
    assert any("only in the event stream" in note for note in found.notes)


# --------------------------------------------------------------------------
# reading: malformed input
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fragment",
    ["unreadable JSON", "line is not an object", "unknown content block type 'hologram'"],
)
def test_every_malformation_is_reported_and_skipped(source: Path, fragment: str) -> None:
    found = edge(source)
    assert any(fragment in warning for warning in found.warnings), found.warnings
    assert found.conversation is not None


def test_unknown_record_and_payload_types_are_ignored_quietly(source: Path) -> None:
    """Codex adds these between releases; they are routine, not faults."""
    found = edge(source)
    assert not any("telepathy" in w or "loom_operation" in w for w in found.warnings)


def test_a_session_with_no_messages_is_skipped(source: Path) -> None:
    found = read_rollout(next((source / "sessions").rglob(f"*{EMPTY_ID}.jsonl")))
    assert found.conversation is None
    assert any("no messages" in note for note in found.notes)


def test_an_inline_image_becomes_an_attachment_in_position(source: Path) -> None:
    found = edge(source)
    assert found.conversation is not None
    blocks = found.conversation.messages[0].content

    assert [b.type for b in blocks] == ["image", "text"]
    assert len(found.attachments) == 1
    assert found.attachments[0].data.startswith(b"\x89PNG")
    assert blocks[0].attachment_id == found.attachments[0].record.id


# --------------------------------------------------------------------------
# detect / export
# --------------------------------------------------------------------------


def test_detect_finds_the_rollouts(adapter: CodexAdapter) -> None:
    result = adapter.detect()

    assert result.installed is True
    # Three rollouts; one holds only session metadata and one is a subagent
    # Codex never lists. A rollout always opens with records that say nothing
    # about whether anyone spoke, and a subagent thread is indistinguishable
    # from a conversation by anything except its header.
    assert result.conversation_count_estimate == 1
    assert any("1 empty" in note for note in result.notes)
    assert result.version == "0.149.0-alpha.4.1"


def test_a_subagent_thread_is_not_counted_as_a_conversation(adapter: CodexAdapter) -> None:
    """Codex writes a subagent its own rollout and never offers it to resume.

    On the developer's machine that was one file in six, so Ferry reported six
    threads against Codex's five. The same gap as Antigravity's six-versus-two
    -- found only because the human asked whether the other three adapters had
    been checked against their own applications rather than against their files.
    """
    result = adapter.detect()

    assert any("1 subagent thread" in note for note in result.notes)
    # Named, not silently subtracted: an unexplained drop from three files to
    # one conversation is its own kind of wrong.
    assert not any(note.startswith("1 ") and note.strip() == "1" for note in result.notes)


def test_a_subagent_is_recognised_by_its_header_not_by_a_parent_id(source: Path) -> None:
    """The real subagent fixture carries no ``parent_thread_id`` at all.

    Its parent is in ``session_id``, which on a subagent holds the spawning
    thread rather than a copy of ``id``. Reading only ``parent_thread_id``
    would have counted this one as a conversation.
    """
    subagent = next(source.rglob(f"*{EDGE_ID}.jsonl"))
    top_level = next(source.rglob(f"*{BASIC_ID}.jsonl"))

    assert parent_thread(subagent) == UUID(PARENT_ID)
    assert parent_thread(top_level) is None


def test_a_file_that_is_not_a_rollout_has_no_parent(tmp_path: Path) -> None:
    absent = tmp_path / "gone.jsonl"
    junk = tmp_path / "junk.jsonl"
    junk.write_text("not json at all" + chr(10), encoding="utf-8")

    assert parent_thread(absent) is None
    assert parent_thread(junk) is None


def test_export_names_a_subagent_and_counts_it_apart(adapter: CodexAdapter, tmp_path: Path) -> None:
    """Both numbers, always.

    The conversation count is the one the user can check against Codex; the
    subagent count explains the extra file in the bundle before they notice it
    and wonder what it is.
    """
    events = list(adapter.export(tmp_path / "bundle"))

    done = next(event for event in events if event.kind == "done")
    assert done.message == "1 of 1 conversations exported, plus 1 subagent thread they spawned"
    assert any(
        event.kind == "progress" and event.message.startswith("subagent of ") for event in events
    )
    # Carried, not dropped. The bundle still holds every file.
    assert sorted(Bundle.open(tmp_path / "bundle").list_conversations()) == sorted(
        [BASIC_ID, EDGE_ID]
    )


def test_detect_on_a_machine_without_codex(tmp_path: Path) -> None:
    result = CodexAdapter(env={"CODEX_HOME": str(tmp_path / "absent")}).detect()

    assert result.installed is False
    assert any("no session directory" in note for note in result.notes)


def test_export_writes_the_sessions_that_had_messages(
    adapter: CodexAdapter, tmp_path: Path
) -> None:
    list(adapter.export(tmp_path / "bundle"))
    bundle = Bundle.open(tmp_path / "bundle")

    assert sorted(bundle.list_conversations()) == sorted([BASIC_ID, EDGE_ID])
    assert bundle.validate() == []


def test_export_does_not_touch_the_source(
    adapter: CodexAdapter, source: Path, tmp_path: Path
) -> None:
    from ferry.core import sha256_file

    before = {p: sha256_file(p) for p in sorted(source.rglob("*")) if p.is_file()}
    list(adapter.export(tmp_path / "bundle"))
    after = {p: sha256_file(p) for p in sorted(source.rglob("*")) if p.is_file()}

    assert after == before


def test_the_reader_never_reads_a_whole_rollout(source: Path, monkeypatch) -> None:
    """Proven structurally, not measured.

    PLAN.md §5 M4 forbids whole-file reads because a rollout can reach hundreds
    of megabytes. A benchmark can only say "it did not this time"; making the
    whole-file APIs raise says it *cannot*.
    """

    def forbidden(self: Path, *args: object, **kwargs: object) -> None:
        raise AssertionError(f"whole-file read of {self.name}")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)

    found = basic(source)
    assert found.conversation is not None
    assert len(found.conversation.messages) == 5


def test_export_is_resumable(adapter: CodexAdapter, tmp_path: Path) -> None:
    list(adapter.export(tmp_path / "bundle"))
    events = list(adapter.export(tmp_path / "bundle"))

    assert [e for e in events if e.kind == "progress"] == []
    assert sum(1 for e in events if e.kind == "skipped") == 3


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------


@pytest.fixture
def exported(adapter: CodexAdapter, tmp_path: Path) -> Path:
    list(adapter.export(tmp_path / "bundle"))
    return tmp_path / "bundle"


def test_import_writes_a_rollout_into_the_date_tree(
    exported: Path, target: CodexAdapter, tmp_path: Path
) -> None:
    list(target.import_(exported, ImportOptions()))

    written = tmp_path / "target" / "sessions" / "2026" / "08" / "01"
    assert [p.name for p in written.glob("*.jsonl")] == [
        f"rollout-2026-08-01T09-00-02-{BASIC_ID}.jsonl"
    ]


def test_a_rebuilt_rollout_starts_with_the_header_codex_demands(
    exported: Path, target: CodexAdapter, tmp_path: Path
) -> None:
    """Codex rejects the whole file if this record is wrong, silently."""
    list(target.import_(exported, ImportOptions()))
    written = next((tmp_path / "target").rglob(f"*{BASIC_ID}.jsonl"))

    records = read_jsonl(written)
    assert records[0]["type"] == "session_meta"
    payload = records[0]["payload"]
    assert isinstance(payload["base_instructions"], dict)
    assert isinstance(payload["context_window"], dict)
    assert payload["id"] == str(BASIC_ID)


def test_a_subagents_parent_link_is_not_overwritten(
    exported: Path, target: CodexAdapter, tmp_path: Path
) -> None:
    """`session_id` is the parent's id on a subagent thread, not a copy of `id`.

    Overwriting it reparented the subagent to itself — caught by the round trip,
    not by any unit test that existed at the time.
    """
    list(target.import_(exported, ImportOptions()))
    written = next((tmp_path / "target").rglob(f"*{EDGE_ID}.jsonl"))

    payload = read_jsonl(written)[0]["payload"]
    assert payload["id"] == str(EDGE_ID)
    assert payload["session_id"] == PARENT_ID


def test_import_rewrites_the_working_directory(
    exported: Path, target: CodexAdapter, tmp_path: Path
) -> None:
    old = BACKSLASH.join(["C:", "Users", "sample", "Projects", "gadget"])
    list(target.import_(exported, ImportOptions(path_remap=((old, "/home/bob/gadget"),))))

    written = next((tmp_path / "target").rglob(f"*{BASIC_ID}.jsonl"))
    assert read_jsonl(written)[0]["payload"]["cwd"] == "/home/bob/gadget"


def test_thinking_goes_back_to_the_event_stream(
    exported: Path, target: CodexAdapter, tmp_path: Path
) -> None:
    """It was read from `event_msg`; writing it as `reasoning` would be a lie."""
    list(target.import_(exported, ImportOptions()))
    written = next((tmp_path / "target").rglob(f"*{BASIC_ID}.jsonl"))

    reasoning = [r for r in read_jsonl(written) if r["payload"].get("type") == "agent_reasoning"]
    assert len(reasoning) == 1
    assert reasoning[0]["type"] == "event_msg"
    assert reasoning[0]["payload"]["text"] == "The module is imported in two places."
    assert not any(r["payload"].get("type") == "reasoning" for r in read_jsonl(written))


def test_a_dry_run_writes_nothing(exported: Path, target: CodexAdapter, tmp_path: Path) -> None:
    events = list(target.import_(exported, ImportOptions(dry_run=True)))

    assert not (tmp_path / "target").exists()
    assert sum(1 for e in events if e.kind == "progress") == 2


def test_a_conversation_with_no_header_gets_one_built(
    target: CodexAdapter, tmp_path: Path, manifest
) -> None:
    """A native conversation whose header did not survive gets one built.

    **This asserted a refusal until 2026-09-06.** The reason was sound when it
    was written: an invented header was believed to make Codex discard the
    conversation with no error, and a silent loss is worse than a refusal.
    Measured again against Codex 0.147, both halves were wrong -- eight derived
    fields are accepted, and a header Codex dislikes is refused *loudly*, by
    name (`Model provider `` not found`). See `SYNTHETIC_HEADER_FIELDS`.
    """
    conversation_id = UUID("019f4444-4444-7444-8444-444444444444")
    bundle = Bundle.create(tmp_path / "thin", manifest)
    bundle.add_conversation(
        Conversation(
            id=conversation_id,
            source_tool="codex",
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
            updated_at=datetime(2026, 8, 1, tzinfo=UTC),
            workspace=Workspace(original_path="/home/bob/thin"),
            messages=[Message(role="user", content=[TextBlock(text="hello")])],
        )
    )

    events = list(target.import_(tmp_path / "thin", ImportOptions()))

    assert sum(1 for e in events if e.kind == "progress") == 1
    written = list((tmp_path / "target").rglob("*.jsonl"))
    assert written, "a conversation with no header was refused instead of built"
    first = json.loads(written[0].read_text(encoding="utf-8").splitlines()[0])
    assert first["type"] == "session_meta"
    assert set(first["payload"]) == set(SYNTHETIC_HEADER_FIELDS)


def test_a_conversation_from_another_tool_is_refused_by_default(
    target: CodexAdapter, tmp_path: Path, manifest
) -> None:
    bundle = Bundle.create(tmp_path / "foreign", manifest)
    bundle.add_conversation(
        Conversation(
            id=UUID("019f5555-5555-7555-8555-555555555555"),
            source_tool="claude-code",
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
            updated_at=datetime(2026, 8, 1, tzinfo=UTC),
            workspace=Workspace(original_path="/home/bob/other"),
            messages=[Message(role="user", content=[TextBlock(text="hello")])],
        )
    )

    events = list(target.import_(tmp_path / "foreign", ImportOptions()))
    assert any(e.kind == "skipped" for e in events)


def test_importing_twice_skips_by_default(
    exported: Path, target: CodexAdapter, tmp_path: Path
) -> None:
    list(target.import_(exported, ImportOptions()))
    written = next((tmp_path / "target").rglob(f"*{BASIC_ID}.jsonl"))
    stamp = written.read_bytes()

    events = list(target.import_(exported, ImportOptions()))

    assert sum(1 for e in events if e.kind == "skipped") == 2
    assert written.read_bytes() == stamp


def test_overwriting_backs_the_old_file_up_first(
    exported: Path, target: CodexAdapter, tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    list(target.import_(exported, ImportOptions()))
    written = next((tmp_path / "target").rglob(f"*{BASIC_ID}.jsonl"))
    written.write_text("clobbered", encoding="utf-8")

    list(target.import_(exported, ImportOptions(on_conflict="overwrite")))

    backups = list((home / ".ferry" / "backups").rglob("*.jsonl"))
    assert any(b.read_text(encoding="utf-8") == "clobbered" for b in backups)
    assert written.read_text(encoding="utf-8") != "clobbered"


def test_a_broken_bundle_is_reported_rather_than_raised(
    target: CodexAdapter, tmp_path: Path
) -> None:
    (tmp_path / "notabundle").mkdir()
    assert [e.kind for e in target.import_(tmp_path / "notabundle", ImportOptions())] == ["error"]


def test_export_reports_an_absent_source_instead_of_raising(tmp_path: Path) -> None:
    absent = CodexAdapter(env={"CODEX_HOME": str(tmp_path / "absent")})
    assert [e.kind for e in absent.export(tmp_path / "bundle")] == ["error"]


def test_a_large_session_is_flagged_before_it_is_read(
    source: Path, tmp_path: Path, monkeypatch
) -> None:
    """PLAN.md §5 M4 asks for a size guard; the user should hear about it first."""
    monkeypatch.setattr("ferry.adapters.codex.adapter.LARGE_SESSION_BYTES", 100)
    adapter = CodexAdapter(env={"CODEX_HOME": str(source)})

    events = list(adapter.export(tmp_path / "bundle"))
    assert any("large session" in e.message for e in events if e.kind == "warning")


def test_a_cross_tool_import_records_where_it_came_from(
    exported: Path, target: CodexAdapter, tmp_path: Path, manifest
) -> None:
    """A foreign conversation is written, and says so.

    This asserted a refusal until 2026-09-06. Now that Codex accepts a built
    header, PLAN.md §3.2 is carried by the record instead of by the format's
    inability: the conversation is written, and a provenance stamp says where
    it came from and what the conversion cost. **The rule was never "refuse",
    it was "never pass a conversion off as native"** -- refusing was simply the
    only way to honour it while the header could not be built.
    """
    conversation_id = UUID("019f6666-6666-7666-8666-666666666666")
    bundle = Bundle.create(tmp_path / "foreign", manifest)
    bundle.add_conversation(
        Conversation(
            id=conversation_id,
            source_tool="claude-code",
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
            updated_at=datetime(2026, 8, 1, tzinfo=UTC),
            workspace=Workspace(original_path="/home/bob/other"),
            messages=[Message(role="user", content=[TextBlock(text="hello")])],
        )
    )

    events = list(target.import_(tmp_path / "foreign", ImportOptions(allow_cross_tool=True)))

    assert sum(1 for e in events if e.kind == "progress") == 1
    recorded = provenance_store.recall("codex", conversation_id)
    assert recorded is not None, "a conversion left no record of itself"
    assert recorded.original_tool == "claude-code"
    assert recorded.imported_into == "codex"
    assert recorded.lossy is True


def test_the_writer_builds_the_measured_header_and_no_more(
    exported: Path, target: CodexAdapter, tmp_path: Path, manifest
) -> None:
    """The writer builds a header, and builds exactly the measured one.

    This asserted the opposite -- that the writer produces nothing without a
    header -- as a guarantee under the compatibility table. The measurement
    that justified it has expired, so what is guarded now is the shape rather
    than the refusal: **eight fields, no more**. Fifteen were refused by Codex
    where eight were accepted, so a field added here in good faith is a way to
    break every conversion at once.
    """
    conversation = Conversation(
        id=UUID("019f7777-7777-7777-8777-777777777777"),
        source_tool="claude-code",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        updated_at=datetime(2026, 8, 1, tzinfo=UTC),
        workspace=Workspace(original_path="/home/bob/other"),
        messages=[Message(role="user", content=[TextBlock(text="hello")])],
    )

    rebuild = Rebuild(cwd="/home/bob/other", thread_id=conversation.id)
    header = session_meta_for(conversation, rebuild)

    assert header is not None
    assert set(header) == set(SYNTHETIC_HEADER_FIELDS)
    assert header["id"] == header["session_id"] == str(conversation.id)
    assert header["cwd"] == "/home/bob/other"
    # Never dressed as a Codex that wrote it. The field is free text, and
    # naming Ferry keeps the standing rule that a conversion is not passed off
    # as native.
    assert header["cli_version"].startswith("ferry-")


def test_detect_survives_an_unreadable_directory(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "root"
    (root / "sessions").mkdir(parents=True)

    def refuse(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError("nope")

    monkeypatch.setattr(Path, "rglob", refuse)
    result = CodexAdapter(env={"CODEX_HOME": str(root)}).detect()

    assert result.installed is False
    assert any("cannot read" in note for note in result.notes)


def test_a_conversation_without_a_workspace_still_lands_somewhere(
    target: CodexAdapter, tmp_path: Path, source: Path
) -> None:
    """Reported, not guessed silently."""
    adapter = CodexAdapter(env={"CODEX_HOME": str(source)})
    list(adapter.export(tmp_path / "bundle"))

    bundle = Bundle.open(tmp_path / "bundle")
    conversation = bundle.load_conversation(BASIC_ID)
    conversation.workspace = Workspace()
    bundle.add_conversation(conversation)

    events = list(target.import_(tmp_path / "bundle", ImportOptions()))
    assert any("records no working directory" in e.message for e in events if e.kind == "warning")


# --------------------------------------------------------------------------
# round trip
# --------------------------------------------------------------------------


def test_export_import_export_gives_back_the_same_conversations(
    exported: Path, target: CodexAdapter, tmp_path: Path
) -> None:
    """The gate. A lossy mapping anywhere shows up as a diff here."""
    list(target.import_(exported, ImportOptions()))
    list(target.export(tmp_path / "again"))

    first, second = Bundle.open(exported), Bundle.open(tmp_path / "again")
    assert first.list_conversations() == second.list_conversations()
    for conversation_id in first.list_conversations():
        before = json.loads(first.load_conversation(conversation_id).model_dump_json())
        after = json.loads(second.load_conversation(conversation_id).model_dump_json())
        assert before == after, conversation_id


def test_the_image_survives_the_round_trip_as_bytes(
    exported: Path, target: CodexAdapter, tmp_path: Path
) -> None:
    list(target.import_(exported, ImportOptions()))
    list(target.export(tmp_path / "again"))

    first, second = Bundle.open(exported), Bundle.open(tmp_path / "again")
    before = first.load_conversation(EDGE_ID).attachments[0]
    after = second.load_conversation(EDGE_ID).attachments[0]

    assert before.sha256 == after.sha256
    assert (exported / before.bundle_path).read_bytes() == (
        tmp_path / "again" / after.bundle_path
    ).read_bytes()


# --------------------------------------------------------------------------
# writer units
# --------------------------------------------------------------------------


def test_the_filename_stamp_has_no_colons() -> None:
    """A colon cannot appear in a Windows filename."""
    assert rollout_stamp(datetime(2026, 8, 1, 9, 30, 15, tzinfo=UTC)) == "2026-08-01T09-30-15"


def test_a_missing_header_is_built_from_the_conversation_and_the_machine() -> None:
    """Built, not guessed, and the distinction is the whole test.

    Nothing here is copied from another session. A header borrowed wholesale
    would carry someone else's git state, instructions and context window, and
    describe work that never happened in this conversation -- which is a
    different and worse failure than having no header at all.
    """
    conversation = Conversation(
        id=BASIC_ID,
        source_tool="codex",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        updated_at=datetime(2026, 8, 1, tzinfo=UTC),
        workspace=Workspace(),
    )

    header = session_meta_for(conversation, Rebuild(cwd="/x", thread_id=BASIC_ID))

    assert header is not None
    assert set(header) == set(SYNTHETIC_HEADER_FIELDS)
    assert header["cwd"] == "/x"
    assert header["timestamp"].startswith("2026-08-01")
    # Codex named this one itself when it was missing, which is how it got here.
    assert header["model_provider"] == "openai"


# --------------------------------------------------------------------------
# pasted-text attachments
# --------------------------------------------------------------------------


def paste(home: Path, attachment_id: str, text: str) -> Path:
    """Lay out a pasted-text attachment the way Codex does."""
    path = home / "attachments" / attachment_id / "pasted-text.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_pasted_file_the_transcript_only_names_in_prose_is_carried(
    source: Path, tmp_path: Path
) -> None:
    """Twelve of twenty-one real ones existed nowhere in any transcript.

    Codex writes pasted text to a file and refers to it by *path inside the
    message*, with no structural field to follow. Leaving them behind loses
    conversation material outright -- the largest on the probe machine was
    3.4 MB of text that appears in no rollout at all.
    """
    paste(source, PARENT_ID, "pasted content that lives only on disk")
    adapter = CodexAdapter(env={"CODEX_HOME": str(source)})

    list(adapter.export(tmp_path / "bundle"))
    bundle = Bundle.open(tmp_path / "bundle")
    conversation = bundle.load_conversation(EDGE_ID)

    pasted = [a for a in conversation.attachments if a.mime_type == "text/plain"]
    assert [a.filename for a in pasted] == [f"{PARENT_ID}/pasted-text.txt"]
    written = tmp_path / "bundle" / pasted[0].bundle_path
    assert written.read_text(encoding="utf-8") == "pasted content that lives only on disk"


def test_a_pasted_file_is_restored_to_the_same_relative_place(
    source: Path, target: CodexAdapter, tmp_path: Path
) -> None:
    paste(source, PARENT_ID, "restore me")
    adapter = CodexAdapter(env={"CODEX_HOME": str(source)})
    list(adapter.export(tmp_path / "bundle"))

    list(target.import_(tmp_path / "bundle", ImportOptions()))

    restored = tmp_path / "target" / "attachments" / PARENT_ID / "pasted-text.txt"
    assert restored.read_text(encoding="utf-8") == "restore me"


def test_a_uuid_that_names_no_attachment_costs_nothing(source: Path, tmp_path: Path) -> None:
    """Every UUID in the prose is looked up; almost none of them are attachments."""
    adapter = CodexAdapter(env={"CODEX_HOME": str(source)})
    list(adapter.export(tmp_path / "bundle"))

    conversation = Bundle.open(tmp_path / "bundle").load_conversation(EDGE_ID)
    assert [a for a in conversation.attachments if a.mime_type == "text/plain"] == []


def test_pasted_files_survive_the_round_trip(
    source: Path, target: CodexAdapter, tmp_path: Path
) -> None:
    paste(source, PARENT_ID, "round trip me")
    adapter = CodexAdapter(env={"CODEX_HOME": str(source)})
    list(adapter.export(tmp_path / "bundle"))
    list(target.import_(tmp_path / "bundle", ImportOptions()))
    list(target.export(tmp_path / "again"))

    first, second = Bundle.open(tmp_path / "bundle"), Bundle.open(tmp_path / "again")
    before = first.load_conversation(EDGE_ID)
    after = second.load_conversation(EDGE_ID)
    assert [a.filename for a in before.attachments] == [a.filename for a in after.attachments]
    assert [a.sha256 for a in before.attachments] == [a.sha256 for a in after.attachments]
