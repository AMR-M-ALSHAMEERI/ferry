"""Removing a conversation from a bundle, and removing a bundle.

This is the only thing Ferry does after which the data is simply gone. Every
other write has a source tool still holding the original, or a backup taken
first; **a bundle is the backup**, so these tests are about what must not be
left behind and what must not be taken by mistake.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from ferry.core import Bundle, BundleError, Manifest, SourceMachine, delete_bundle
from ferry.ucs import Attachment, Conversation, Message, TextBlock, Workspace

ONE = UUID("11111111-1111-4111-8111-111111111111")
TWO = UUID("22222222-2222-4222-8222-222222222222")


def _conversation(conversation_id: UUID, tool: str = "codex") -> Conversation:
    return Conversation(
        id=conversation_id,
        source_tool=tool,  # type: ignore[arg-type]
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        updated_at=datetime(2026, 8, 2, tzinfo=UTC),
        title=f"conversation {str(conversation_id)[:4]}",
        workspace=Workspace(original_path="/home/sample/work"),
        messages=[Message(role="user", content=[TextBlock(text="hello")])],
    )


@pytest.fixture
def bundle(tmp_path: Path) -> Bundle:
    """Two conversations from two tools, one carrying baggage.

    ``ONE`` has an attachment and an original file, because the fault this
    guards against is deleting the conversation document and leaving those.
    """
    made = Bundle.create(
        tmp_path / "bundle",
        Manifest(
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
            created_by="ferry test",
            source_machine=SourceMachine(os="linux", user_home="/home/sample"),
        ),
    )
    first = _conversation(ONE, "codex")
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
    original.write_bytes(b"the database this came from" * 100)
    made.add_source_raw(ONE, original)

    made.add_conversation(_conversation(TWO, "claude-code"))
    return made


@pytest.fixture(autouse=True)
def _backups_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the delete backups off the developer's real home directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


class TestDeleteConversation:
    def test_a_conversation_is_not_one_file(self, bundle: Bundle) -> None:
        """The document, its attachments and the original it came from."""
        files = bundle.conversation_files(ONE)

        assert bundle.conversation_path(ONE) in files
        assert bundle.source_raw_path(ONE) in files
        assert any("attachments" in str(path) for path in files)

    def test_everything_belonging_to_it_goes(self, bundle: Bundle) -> None:
        """Leaving the attachments behind orphans megabytes silently.

        For an Antigravity conversation the original database is most of its
        size, so a delete that removes only the UCS document frees almost
        nothing while reporting success.
        """
        bundle.delete_conversation(ONE)

        assert not bundle.conversation_path(ONE).is_file()
        assert not bundle.source_raw_path(ONE).is_file()
        assert not (bundle.root / "attachments" / str(ONE)).exists()

    def test_the_other_conversation_is_untouched(self, bundle: Bundle) -> None:
        before = bundle.conversation_path(TWO).read_bytes()

        bundle.delete_conversation(ONE)

        assert bundle.conversation_path(TWO).read_bytes() == before

    def test_the_bundle_still_validates_afterwards(self, bundle: Bundle) -> None:
        """The manifest count and the files must move together.

        A count left behind is exactly the state a half-finished delete leaves,
        which is why it is checked rather than assumed.
        """
        bundle.delete_conversation(ONE)

        assert bundle.manifest.conversation_count == 1
        assert bundle.validate() == []

    def test_a_tool_with_nothing_left_stops_being_listed(self, bundle: Bundle) -> None:
        assert set(bundle.manifest.tools_included) == {"codex", "claude-code"}

        bundle.delete_conversation(ONE)

        assert bundle.manifest.tools_included == ["claude-code"]

    def test_a_copy_is_kept_by_default(self, bundle: Bundle, tmp_path: Path) -> None:
        """Unlike an import, nothing else holds this data once it is gone."""
        removed = bundle.delete_conversation(ONE)

        assert removed.backup is not None
        kept = list((tmp_path / "home" / ".ferry" / "backups").rglob(f"{ONE}.json"))
        assert kept, "the conversation document must be in the backup"

    def test_the_copy_can_be_declined(self, bundle: Bundle, tmp_path: Path) -> None:
        removed = bundle.delete_conversation(ONE, backup=False)

        assert removed.backup is None
        assert not (tmp_path / "home" / ".ferry" / "backups").exists()

    def test_it_reports_what_it_freed(self, bundle: Bundle) -> None:
        expected = sum(path.stat().st_size for path in bundle.conversation_files(ONE))

        removed = bundle.delete_conversation(ONE)

        assert removed.conversations == 1
        assert removed.files >= 3
        assert removed.bytes_freed == expected

    def test_deleting_something_that_is_not_there_is_refused(self, bundle: Bundle) -> None:
        with pytest.raises(BundleError, match="not in bundle"):
            bundle.delete_conversation(UUID("44444444-4444-4444-8444-444444444444"))

    def test_a_backup_that_fails_stops_the_delete(
        self, bundle: Bundle, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A backup that failed is a reason to stop, not a step to skip."""

        def refuse(*args: object, **kwargs: object) -> Path:
            raise OSError("no room")

        monkeypatch.setattr("ferry.core.backup.back_up", refuse)

        with pytest.raises(BundleError, match="nothing was removed"):
            bundle.delete_conversation(ONE)

        assert bundle.conversation_path(ONE).is_file()


class TestDeleteBundle:
    def test_the_whole_directory_goes(self, bundle: Bundle) -> None:
        removed = delete_bundle(bundle.root)

        assert not bundle.root.exists()
        assert removed.conversations == 2
        assert removed.bytes_freed > 0

    def test_a_directory_that_is_not_a_bundle_is_refused(self, tmp_path: Path) -> None:
        """The one check standing between a mistyped path and someone's documents.

        This is the only place Ferry removes a directory tree it did not
        create, so the refusal is the feature.
        """
        ordinary = tmp_path / "Documents"
        ordinary.mkdir()
        (ordinary / "thesis.docx").write_text("years of work", encoding="utf-8")

        with pytest.raises(BundleError, match="not a bundle"):
            delete_bundle(ordinary)

        assert (ordinary / "thesis.docx").is_file()

    def test_no_copy_is_taken_unless_it_is_asked_for(self, bundle: Bundle, tmp_path: Path) -> None:
        """A whole bundle is routinely gigabytes.

        Copying one in order to delete it is not a backup so much as a rename
        the user did not ask for, so this one defaults the other way from
        deleting a single conversation.
        """
        delete_bundle(bundle.root)

        assert not (tmp_path / "home" / ".ferry" / "backups").exists()

    def test_a_copy_is_taken_when_it_is(self, bundle: Bundle, tmp_path: Path) -> None:
        removed = delete_bundle(bundle.root, backup=True)

        assert removed.backup is not None
        assert list((tmp_path / "home" / ".ferry" / "backups").rglob("manifest.json"))
