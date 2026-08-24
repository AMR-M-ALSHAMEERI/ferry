"""Describing a bundle without importing it.

Until this existed, the only way to find out what you had backed up was to
import it somewhere and look. The manifest could say "5 conversations from
codex"; nothing could say what they were, when they happened, or which folders
they expected to find.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from ferry.core import Bundle, Manifest, SourceMachine, summarise
from ferry.ucs import Attachment, Conversation, Message, TextBlock, Workspace

ONE = UUID("11111111-1111-4111-8111-111111111111")
TWO = UUID("22222222-2222-4222-8222-222222222222")


def _conversation(
    conversation_id: UUID, tool: str, title: str | None, folder: str, messages: int = 1
) -> Conversation:
    return Conversation(
        id=conversation_id,
        source_tool=tool,  # type: ignore[arg-type]
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        updated_at=datetime(2026, 8, 2, tzinfo=UTC),
        title=title,
        workspace=Workspace(original_path=folder),
        messages=[Message(role="user", content=[TextBlock(text="hello")]) for _ in range(messages)],
    )


@pytest.fixture
def bundle(tmp_path: Path) -> Bundle:
    made = Bundle.create(
        tmp_path / "bundle",
        Manifest(
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
            created_by="ferry test",
            source_machine=SourceMachine(hostname="workshop", os="linux", user_home="/home/sample"),
        ),
    )
    first = _conversation(ONE, "codex", "Fix the parser", "/home/sample/work", messages=3)
    payload = b"an image, notionally"
    attachment = Attachment(
        id=UUID("33333333-3333-4333-8333-333333333333"),
        filename="shot.png",
        mime_type="image/png",
        bundle_path=f"attachments/{ONE}/33333333-3333-4333-8333-333333333333.png",
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    made.add_attachment_bytes(ONE, payload, attachment)
    first.attachments.append(attachment)
    made.add_conversation(first)

    original = tmp_path / "original.db"
    original.write_bytes(b"the database this came from")
    made.add_source_raw(ONE, original)

    made.add_conversation(_conversation(TWO, "claude-code", None, "/home/sample/other"))
    return made


def test_each_conversation_is_described(bundle: Bundle) -> None:
    summary = summarise(bundle)

    first = next(c for c in summary.conversations if c.id == ONE)
    assert first.title == "Fix the parser"
    assert first.tool == "codex"
    assert first.messages == 3
    assert first.attachments == 1
    assert first.has_source_raw is True
    assert first.workspace == "/home/sample/work"


def test_an_untitled_conversation_still_has_something_to_call_it(bundle: Bundle) -> None:
    """A list row that is blank is a row nobody can pick from."""
    summary = summarise(bundle)

    second = next(c for c in summary.conversations if c.id == TWO)
    assert second.title is None
    assert second.name == f"untitled ({str(TWO)[:8]})"


def test_the_size_is_what_deleting_it_would_free(bundle: Bundle) -> None:
    """The document alone is not the answer.

    For an Antigravity conversation the original database is most of its
    weight, so a size that counted only the UCS file would tell someone
    deciding what to delete almost nothing.
    """
    summary = summarise(bundle)

    first = next(c for c in summary.conversations if c.id == ONE)
    expected = sum(path.stat().st_size for path in bundle.conversation_files(ONE))
    assert first.bytes_on_disk == expected
    assert first.bytes_on_disk > bundle.conversation_path(ONE).stat().st_size


def test_the_recorded_folders_are_listed_once_each(bundle: Bundle) -> None:
    """What the import screen cannot afford to compute.

    Someone restoring onto another machine needs to see which folders a bundle
    expects before being asked where those folders now live.
    """
    summary = summarise(bundle)

    assert summary.folders == ["/home/sample/work", "/home/sample/other"]


def test_conversations_are_counted_by_tool(bundle: Bundle) -> None:
    assert summarise(bundle).by_tool == {"codex": 1, "claude-code": 1}


def test_a_conversation_that_cannot_be_read_is_still_listed(bundle: Bundle) -> None:
    """A bundle you cannot read is the one you most need to look at.

    Summarising through the UCS models would have raised here and taken the
    whole screen with it -- including the rows for every conversation that is
    perfectly fine, and including the row for the broken one, which is the
    thing you would want to delete.
    """
    bundle.conversation_path(TWO).write_text("{ not json", encoding="utf-8")

    summary = summarise(bundle)

    broken = next(c for c in summary.conversations if c.id == TWO)
    assert broken.unreadable is not None
    assert broken.name.startswith("untitled")
    assert len(summary.conversations) == 2


def test_problems_are_reported_rather_than_hidden(bundle: Bundle) -> None:
    bundle.manifest.conversation_count = 99
    bundle._write_manifest()

    summary = summarise(Bundle.open(bundle.root))

    assert any("99" in problem for problem in summary.problems)


def test_validation_can_be_skipped(bundle: Bundle) -> None:
    bundle.manifest.conversation_count = 99
    bundle._write_manifest()

    assert summarise(Bundle.open(bundle.root), validate=False).problems == []


def test_a_conversation_file_that_is_not_an_object_is_reported_not_crashed(
    bundle: Bundle,
) -> None:
    bundle.conversation_path(TWO).write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    broken = next(c for c in summarise(bundle).conversations if c.id == TWO)

    assert broken.unreadable is not None
    assert "not a JSON object" in broken.unreadable
