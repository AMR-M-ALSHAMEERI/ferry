"""Sealing a bundle into one encrypted file, and opening it again.

The whole feature is worthless if a sealed bundle cannot be reopened, and the
failure is invisible until the day someone needs it. So the round trip is
checked against the *conversations*, not against the bytes: a file that
decrypts to a corrupt zip would pass a byte comparison of the sealed file and
fail the only test that matters.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from ferry.core import Bundle, BundleError, Manifest, SourceMachine
from ferry.core.crypto import WrongPassphrase
from ferry.core.sealed import (
    SEALED_MAGIC,
    SEALED_SUFFIX,
    is_sealed,
    opens_with,
    read_sealed_params,
    seal_bundle,
    unseal_bundle,
    unsealed,
)
from ferry.ucs import Attachment, Conversation, Message, TextBlock, Workspace

PASSPHRASE = "a passphrase nobody else knows"
ONE = UUID("11111111-1111-4111-8111-111111111111")
TWO = UUID("22222222-2222-4222-8222-222222222222")


def _conversation(conversation_id: UUID, title: str) -> Conversation:
    return Conversation(
        id=conversation_id,
        source_tool="codex",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        updated_at=datetime(2026, 8, 2, tzinfo=UTC),
        title=title,
        workspace=Workspace(original_path="/home/sample/work"),
        messages=[Message(role="user", content=[TextBlock(text="something private")])],
    )


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    made = Bundle.create(
        tmp_path / "plain-bundle",
        Manifest(
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
            created_by="ferry test",
            source_machine=SourceMachine(hostname="workshop", os="linux", user_home="/home/sample"),
        ),
    )
    made.add_conversation(_conversation(ONE, "Fix the parser"))
    payload = b"an image, notionally"
    attachment = Attachment(
        id=UUID("33333333-3333-4333-8333-333333333333"),
        filename="shot.png",
        mime_type="image/png",
        bundle_path=f"attachments/{ONE}/33333333-3333-4333-8333-333333333333.png",
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    made.add_attachment_bytes(ONE, payload, attachment)
    made.add_conversation(_conversation(TWO, "Review the README"))
    return made.root


@pytest.fixture(autouse=True)
def _home_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the unsealed working copy off the developer's real home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


class TestRoundTrip:
    def test_a_sealed_bundle_opens_to_the_same_conversations(
        self, bundle: Path, tmp_path: Path
    ) -> None:
        """The only test that matters, and it checks content rather than bytes."""
        sealed = seal_bundle(bundle, PASSPHRASE)

        opened = unseal_bundle(sealed.path, PASSPHRASE, tmp_path / "opened")

        assert sorted(opened.list_conversations()) == sorted([ONE, TWO])
        assert opened.load_conversation(ONE).title == "Fix the parser"
        assert opened.validate() == []

    def test_the_attachment_survives(self, bundle: Path, tmp_path: Path) -> None:
        sealed = seal_bundle(bundle, PASSPHRASE)

        opened = unseal_bundle(sealed.path, PASSPHRASE, tmp_path / "opened")

        stored = opened.root / f"attachments/{ONE}/33333333-3333-4333-8333-333333333333.png"
        assert stored.read_bytes() == b"an image, notionally"

    def test_nothing_about_it_is_readable_from_the_file(self, bundle: Path) -> None:
        """Not the titles, not the tool, not the count.

        A sealed bundle is one encrypted file precisely so that what it holds is
        not visible to someone who finds it.
        """
        sealed = seal_bundle(bundle, PASSPHRASE)

        body = sealed.path.read_bytes()
        assert b"Fix the parser" not in body
        assert b"something private" not in body
        assert b"codex" not in body
        assert b"conversations" not in body

    def test_it_says_what_it_is(self, bundle: Path) -> None:
        sealed = seal_bundle(bundle, PASSPHRASE)

        assert sealed.path.name.endswith(SEALED_SUFFIX)
        assert sealed.path.read_bytes().startswith(SEALED_MAGIC)
        assert is_sealed(sealed.path)
        assert sealed.conversations == 2

    def test_the_original_is_left_alone(self, bundle: Path) -> None:
        """Deleting someone's only copy as a side effect is not sealing's call."""
        before = sorted(p.name for p in bundle.rglob("*") if p.is_file())

        seal_bundle(bundle, PASSPHRASE)

        assert sorted(p.name for p in bundle.rglob("*") if p.is_file()) == before

    def test_sealing_leaves_no_working_files_behind(self, bundle: Path) -> None:
        """The zip exists only to be encrypted."""
        seal_bundle(bundle, PASSPHRASE)

        strays = [p for p in bundle.parent.iterdir() if p.name.startswith("ferry-")]
        assert strays == []
        assert not list(bundle.parent.glob("*.zip"))


class TestItRefusesWhatItShould:
    def test_the_wrong_passphrase_does_not_open_it(self, bundle: Path, tmp_path: Path) -> None:
        sealed = seal_bundle(bundle, PASSPHRASE)

        with pytest.raises(WrongPassphrase):
            unseal_bundle(sealed.path, "not it", tmp_path / "opened")

    def test_nothing_is_left_behind_when_it_will_not_open(
        self, bundle: Path, tmp_path: Path
    ) -> None:
        sealed = seal_bundle(bundle, PASSPHRASE)
        destination = tmp_path / "opened"

        with pytest.raises(WrongPassphrase):
            unseal_bundle(sealed.path, "not it", destination)

        assert not destination.exists()
        assert not [p for p in tmp_path.iterdir() if p.name.startswith("ferry-unseal")]

    def test_an_altered_file_is_refused(self, bundle: Path, tmp_path: Path) -> None:
        sealed = seal_bundle(bundle, PASSPHRASE)
        body = bytearray(sealed.path.read_bytes())
        body[-1] ^= 0xFF
        sealed.path.write_bytes(bytes(body))

        with pytest.raises(WrongPassphrase):
            unseal_bundle(sealed.path, PASSPHRASE, tmp_path / "opened")

    def test_sealing_without_a_passphrase_is_refused(self, bundle: Path) -> None:
        """An empty passphrase is not encryption, it is a rename."""
        with pytest.raises(BundleError, match="no recovery"):
            seal_bundle(bundle, "")

    def test_sealing_something_that_is_not_a_bundle_is_refused(self, tmp_path: Path) -> None:
        ordinary = tmp_path / "Documents"
        ordinary.mkdir()

        with pytest.raises(BundleError, match="not a bundle"):
            seal_bundle(ordinary, PASSPHRASE)

    def test_a_file_that_was_never_sealed_says_so(self, tmp_path: Path) -> None:
        plain = tmp_path / "notes.txt"
        plain.write_text("hello", encoding="utf-8")

        assert not is_sealed(plain)
        with pytest.raises(BundleError, match="not a sealed Ferry bundle"):
            read_sealed_params(plain)


class TestOpensWith:
    def test_it_confirms_the_right_passphrase(self, bundle: Path) -> None:
        """Read whole, because a passphrase that opens the first frame and
        fails on the last has not opened the bundle."""
        sealed = seal_bundle(bundle, PASSPHRASE)

        assert opens_with(sealed.path, PASSPHRASE)

    def test_it_rejects_the_wrong_one(self, bundle: Path) -> None:
        sealed = seal_bundle(bundle, PASSPHRASE)

        assert not opens_with(sealed.path, "not it")

    def test_it_says_no_rather_than_raising_on_a_file_that_is_not_sealed(
        self, tmp_path: Path
    ) -> None:
        plain = tmp_path / "notes.txt"
        plain.write_text("hello", encoding="utf-8")

        assert not opens_with(plain, PASSPHRASE)


class TestUnsealedContext:
    def test_the_copy_is_removed_afterwards(self, bundle: Path, tmp_path: Path) -> None:
        sealed = seal_bundle(bundle, PASSPHRASE)

        with unsealed(sealed.path, PASSPHRASE) as opened:
            root = opened.root
            assert sorted(opened.list_conversations()) == sorted([ONE, TWO])
            assert root.is_dir()

        assert not root.exists()

    def test_the_copy_is_removed_even_when_the_block_fails(
        self, bundle: Path, tmp_path: Path
    ) -> None:
        """Otherwise a crash mid-import leaves an unencrypted bundle lying about."""
        sealed = seal_bundle(bundle, PASSPHRASE)
        seen: list[Path] = []

        with pytest.raises(RuntimeError), unsealed(sealed.path, PASSPHRASE) as opened:
            seen.append(opened.root)
            raise RuntimeError("something went wrong mid-import")

        assert seen and not seen[0].exists()

    def test_the_copy_lives_under_the_users_own_directory(
        self, bundle: Path, tmp_path: Path
    ) -> None:
        """Not the system temp directory, which is often world-listable."""
        sealed = seal_bundle(bundle, PASSPHRASE)

        with unsealed(sealed.path, PASSPHRASE) as opened:
            assert (tmp_path / "home" / ".ferry" / "open") in opened.root.parents
