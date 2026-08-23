"""Bundle tests, per PLAN.md M1 and §6.8.1.

The round-trip test reloads with an independent parser (json.loads) rather than
trusting the writer's own logic to confirm its own output.
"""

import hashlib
import json
import zipfile
from pathlib import Path
from uuid import UUID

import pytest

from ferry.core import Bundle, BundleError, Manifest
from ferry.ucs import Attachment, Conversation

CONV_ID = UUID("11111111-1111-4111-8111-111111111111")


def _write_png(path: Path) -> str:
    """Write a small binary file and return its real sha256."""
    payload = b"\x89PNG\r\n\x1a\n" + b"synthetic-bytes" * 8
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


# ---------- create / open ----------


def test_create_writes_manifest(tmp_path: Path, manifest: Manifest) -> None:
    bundle = Bundle.create(tmp_path / "b", manifest)
    manifest_file = bundle.root / "manifest.json"
    assert manifest_file.is_file()
    on_disk = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert on_disk["bundle_version"] == "1.0"
    assert on_disk["created_by"] == "ferry v0.1.0"
    assert on_disk["conversation_count"] == 0


def test_create_refuses_nonempty_dir_without_force(tmp_path: Path, manifest: Manifest) -> None:
    root = tmp_path / "b"
    root.mkdir()
    (root / "stray.txt").write_text("existing data", encoding="utf-8")
    with pytest.raises(BundleError, match="already exists"):
        Bundle.create(root, manifest)


def test_open_rejects_directory_without_manifest(tmp_path: Path) -> None:
    (tmp_path / "notabundle").mkdir()
    with pytest.raises(BundleError, match="not a bundle"):
        Bundle.open(tmp_path / "notabundle")


def test_open_rejects_corrupt_manifest(tmp_path: Path, manifest: Manifest) -> None:
    bundle = Bundle.create(tmp_path / "b", manifest)
    (bundle.root / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(BundleError, match="not a valid manifest"):
        Bundle.open(bundle.root)


# ---------- conversations ----------


def test_add_conversation_updates_manifest_counts(
    tmp_path: Path, manifest: Manifest, conversation: Conversation
) -> None:
    bundle = Bundle.create(tmp_path / "b", manifest)
    bundle.add_conversation(conversation)

    assert bundle.list_conversations() == [CONV_ID]
    on_disk = json.loads((bundle.root / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk["conversation_count"] == 1
    assert on_disk["tools_included"] == ["claude-code"]


def test_has_conversation_supports_resumable_export(
    tmp_path: Path, manifest: Manifest, conversation: Conversation
) -> None:
    """Adapters must be able to skip already-written files when resuming (PLAN.md §4)."""
    bundle = Bundle.create(tmp_path / "b", manifest)
    assert bundle.has_conversation(CONV_ID) is False
    bundle.add_conversation(conversation)
    assert bundle.has_conversation(CONV_ID) is True


def test_load_missing_conversation_raises(tmp_path: Path, manifest: Manifest) -> None:
    bundle = Bundle.create(tmp_path / "b", manifest)
    with pytest.raises(BundleError, match="not in bundle"):
        bundle.load_conversation(CONV_ID)


def test_load_invalid_ucs_raises(
    tmp_path: Path, manifest: Manifest, conversation: Conversation
) -> None:
    bundle = Bundle.create(tmp_path / "b", manifest)
    path = bundle.add_conversation(conversation)
    path.write_text('{"ucs_version": "1.2"}', encoding="utf-8")
    with pytest.raises(BundleError, match="not valid UCS"):
        bundle.load_conversation(CONV_ID)


# ---------- round trip ----------


def test_full_round_trip_preserves_content(
    tmp_path: Path, manifest: Manifest, conversation: Conversation
) -> None:
    """create -> add -> pack -> unpack -> open -> load, compared field by field."""
    bundle = Bundle.create(tmp_path / "build", manifest)
    bundle.add_conversation(conversation)
    archive = bundle.pack(tmp_path / "out.zip")

    assert zipfile.is_zipfile(archive)

    reopened = Bundle.unpack(archive, tmp_path / "extracted")
    assert reopened.list_conversations() == [CONV_ID]

    restored = reopened.load_conversation(CONV_ID)
    assert restored.id == conversation.id
    assert restored.title == conversation.title
    assert restored.source_tool == conversation.source_tool
    assert restored.created_at == conversation.created_at
    assert len(restored.messages) == 3
    assert [b.type for b in restored.messages[1].content] == ["thinking", "text", "tool_use"]
    assert restored.messages[2].timestamp is None

    # Independent comparison: raw JSON, not the writer's own equality logic.
    original_json = json.loads(conversation.model_dump_json())
    restored_json = json.loads(restored.model_dump_json())
    assert original_json == restored_json


def test_pack_refuses_overwrite_without_force(
    tmp_path: Path, manifest: Manifest, conversation: Conversation
) -> None:
    """PLAN.md §6.1: refuse to overwrite a bundle file without force."""
    bundle = Bundle.create(tmp_path / "build", manifest)
    bundle.add_conversation(conversation)
    dest = tmp_path / "out.zip"
    bundle.pack(dest)
    with pytest.raises(BundleError, match="already exists"):
        bundle.pack(dest)
    assert bundle.pack(dest, force=True) == dest


def test_unpack_rejects_non_zip(tmp_path: Path) -> None:
    fake = tmp_path / "fake.zip"
    fake.write_text("not a zip", encoding="utf-8")
    with pytest.raises(BundleError, match="not a zip file"):
        Bundle.unpack(fake, tmp_path / "dest")


def test_unpack_refuses_path_traversal(tmp_path: Path) -> None:
    """A malicious bundle must not write outside the destination directory."""
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escaped.txt", "should never be written")
    with pytest.raises(BundleError, match="escapes destination"):
        Bundle.unpack(archive, tmp_path / "dest")
    assert not (tmp_path / "escaped.txt").exists()


# ---------- attachments ----------


def test_add_attachment_copies_and_verifies(
    tmp_path: Path, manifest: Manifest, conversation: Conversation, attachment_record: Attachment
) -> None:
    bundle = Bundle.create(tmp_path / "b", manifest)
    source = tmp_path / "src" / "shot.png"
    attachment_record.sha256 = _write_png(source)
    conversation.attachments = [attachment_record]
    bundle.add_conversation(conversation)

    dest = bundle.add_attachment(CONV_ID, source, attachment_record)
    assert dest.is_file()
    assert dest.read_bytes() == source.read_bytes()
    assert bundle.validate() == []


def test_add_attachment_detects_wrong_checksum(
    tmp_path: Path, manifest: Manifest, attachment_record: Attachment
) -> None:
    bundle = Bundle.create(tmp_path / "b", manifest)
    source = tmp_path / "src" / "shot.png"
    _write_png(source)
    attachment_record.sha256 = "0" * 64
    with pytest.raises(BundleError, match="checksum mismatch"):
        bundle.add_attachment(CONV_ID, source, attachment_record)


def test_add_attachment_rejects_path_outside_its_conversation(
    tmp_path: Path, manifest: Manifest, attachment_record: Attachment
) -> None:
    bundle = Bundle.create(tmp_path / "b", manifest)
    source = tmp_path / "src" / "shot.png"
    attachment_record.sha256 = _write_png(source)
    attachment_record.bundle_path = "../outside.png"
    with pytest.raises(BundleError, match="does not sit under"):
        bundle.add_attachment(CONV_ID, source, attachment_record)


def test_add_attachment_missing_source_raises(
    tmp_path: Path, manifest: Manifest, attachment_record: Attachment
) -> None:
    bundle = Bundle.create(tmp_path / "b", manifest)
    with pytest.raises(BundleError, match="does not exist"):
        bundle.add_attachment(CONV_ID, tmp_path / "nope.png", attachment_record)


# ---------- validate ----------


def test_validate_clean_bundle_returns_no_problems(
    tmp_path: Path, manifest: Manifest, conversation: Conversation
) -> None:
    bundle = Bundle.create(tmp_path / "b", manifest)
    bundle.add_conversation(conversation)
    assert bundle.validate() == []


def test_validate_detects_missing_attachment_file(
    tmp_path: Path, manifest: Manifest, conversation: Conversation, attachment_record: Attachment
) -> None:
    bundle = Bundle.create(tmp_path / "b", manifest)
    source = tmp_path / "src" / "shot.png"
    attachment_record.sha256 = _write_png(source)
    conversation.attachments = [attachment_record]
    bundle.add_conversation(conversation)
    # conversation references the attachment, but the bytes were never added
    problems = bundle.validate()
    assert len(problems) == 1
    assert "missing at" in problems[0]


def test_validate_detects_corrupted_attachment(
    tmp_path: Path, manifest: Manifest, conversation: Conversation, attachment_record: Attachment
) -> None:
    bundle = Bundle.create(tmp_path / "b", manifest)
    source = tmp_path / "src" / "shot.png"
    attachment_record.sha256 = _write_png(source)
    conversation.attachments = [attachment_record]
    bundle.add_conversation(conversation)
    dest = bundle.add_attachment(CONV_ID, source, attachment_record)

    dest.write_bytes(b"corrupted after the fact")
    problems = bundle.validate()
    assert len(problems) == 1
    assert "checksum mismatch" in problems[0]


def test_validate_detects_count_mismatch(
    tmp_path: Path, manifest: Manifest, conversation: Conversation
) -> None:
    bundle = Bundle.create(tmp_path / "b", manifest)
    bundle.add_conversation(conversation)
    bundle.manifest.conversation_count = 7
    bundle._write_manifest()
    problems = bundle.validate()
    assert any("manifest says 7 conversations, found 1" in p for p in problems)


def test_validate_detects_id_filename_disagreement(
    tmp_path: Path, manifest: Manifest, conversation: Conversation
) -> None:
    """A conversation renamed on disk must be caught, not silently trusted."""
    bundle = Bundle.create(tmp_path / "b", manifest)
    path = bundle.add_conversation(conversation)
    other = bundle.root / "conversations" / "44444444-4444-4444-8444-444444444444.json"
    path.rename(other)
    problems = bundle.validate()
    assert any("filename and id disagree" in p for p in problems)


def test_validate_detects_corrupt_conversation_json(
    tmp_path: Path, manifest: Manifest, conversation: Conversation
) -> None:
    bundle = Bundle.create(tmp_path / "b", manifest)
    path = bundle.add_conversation(conversation)
    path.write_text("{ broken", encoding="utf-8")
    problems = bundle.validate()
    assert any("not valid UCS" in p for p in problems)


def test_the_streamed_document_matches_pydantics_own(tmp_path, manifest, conversation) -> None:
    """The bundle does not call ``model_dump_json`` on a whole conversation.

    Doing so peaked at 158 MB to produce 19 MB of JSON for a large Codex
    conversation, which was what limited how big a transcript Ferry could
    handle. Messages are written one at a time instead, at 8 MB. That is only
    safe while the streamed document says exactly what pydantic would have, so
    this test holds the two together -- including for fields added later, which
    is the way a hand-rolled writer normally rots.
    """
    import json as _json

    bundle = Bundle.create(tmp_path / "b", manifest)
    written = bundle.add_conversation(conversation)

    assert _json.loads(written.read_text(encoding="utf-8")) == _json.loads(
        conversation.model_dump_json()
    )


def test_a_conversation_with_no_messages_still_writes_valid_json(
    tmp_path, manifest, conversation
) -> None:
    """The envelope splice has an empty-list edge that a comma would break."""
    import json as _json

    bundle = Bundle.create(tmp_path / "b", manifest)
    empty = conversation.model_copy(update={"messages": []})
    written = bundle.add_conversation(empty)

    assert _json.loads(written.read_text(encoding="utf-8"))["messages"] == []
    assert bundle.load_conversation(empty.id).messages == []


def test_a_conversation_written_to_a_bundle_reads_back_identical(
    tmp_path, manifest, conversation
) -> None:
    """End to end, through the serialiser the bundle actually uses."""
    bundle = Bundle.create(tmp_path / "b", manifest)
    bundle.add_conversation(conversation)

    restored = bundle.load_conversation(conversation.id)
    assert restored.model_dump() == conversation.model_dump()
