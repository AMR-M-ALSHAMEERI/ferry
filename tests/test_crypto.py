"""Encrypting the files inside a bundle.

The failure mode here is unlike anything else in Ferry. A wrong path is
visible; a lost message is countable; **a subtly wrong encryption produces
files that look perfectly encrypted and cannot be opened again, and nobody
finds out until the day the backup is needed.** So these tests are less about
what works and more about what must not be possible: a file that decrypts under
the wrong passphrase, a truncated file that opens as if complete, and a failure
that leaves half a plaintext behind.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from ferry.core.crypto import (
    CHUNK_BYTES,
    MAGIC,
    EncryptionParams,
    WrongPassphrase,
    decrypt_file,
    derive_key,
    encrypt_file,
    is_encrypted,
    new_params,
)

PASSPHRASE = "correct horse battery staple"

_HEADER = 8 + 2 + 4
"""Magic, version, chunk exponent, nonce prefix."""

_FRAME = 4 + CHUNK_BYTES + 16
"""Length prefix, one full frame of ciphertext, its tag."""


@pytest.fixture(scope="module")
def params() -> EncryptionParams:
    return new_params()


@pytest.fixture(scope="module")
def key(params: EncryptionParams) -> bytes:
    """Derived once. scrypt is deliberately slow, and this is a test suite."""
    return derive_key(PASSPHRASE, params)


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _round_trip(tmp_path: Path, key: bytes, data: bytes) -> bytes:
    plain = _write(tmp_path / "plain.bin", data)
    sealed = tmp_path / "sealed.enc"
    encrypt_file(plain, sealed, key)
    opened = tmp_path / "opened.bin"
    decrypt_file(sealed, opened, key)
    return opened.read_bytes()


class TestRoundTrip:
    @pytest.mark.parametrize(
        ("label", "size"),
        [
            ("empty", 0),
            ("one byte", 1),
            ("under one frame", 1000),
            ("exactly one frame", CHUNK_BYTES),
            ("one frame plus one byte", CHUNK_BYTES + 1),
            ("several frames", CHUNK_BYTES * 3 + 17),
        ],
    )
    def test_what_goes_in_comes_out(
        self, tmp_path: Path, key: bytes, label: str, size: int
    ) -> None:
        """The frame boundaries are where a chunked format goes wrong.

        A file that is exactly one frame, or one byte past one, is the case
        that produces an empty final frame or a missing one -- and either
        silently loses the end of a conversation.
        """
        data = bytes((index * 7 + 11) % 256 for index in range(size))

        assert _round_trip(tmp_path, key, data) == data, label

    def test_the_ciphertext_does_not_contain_the_plaintext(
        self, tmp_path: Path, key: bytes
    ) -> None:
        """The floor. Everything else is meaningless without it."""
        secret = b"the passphrase to my bank is hunter2" * 100
        plain = _write(tmp_path / "plain.bin", secret)
        sealed = tmp_path / "sealed.enc"
        encrypt_file(plain, sealed, key)

        body = sealed.read_bytes()
        assert b"hunter2" not in body
        assert secret[:32] not in body

    def test_an_encrypted_file_announces_itself(self, tmp_path: Path, key: bytes) -> None:
        """So a bundle whose manifest was lost still reads as encrypted.

        The difference between "find your passphrase" and "this file is
        ruined".
        """
        plain = _write(tmp_path / "plain.bin", b"hello")
        sealed = tmp_path / "sealed.enc"
        encrypt_file(plain, sealed, key)

        assert sealed.read_bytes().startswith(MAGIC)
        assert is_encrypted(sealed)
        assert not is_encrypted(plain)

    def test_encrypting_twice_gives_different_bytes(self, tmp_path: Path, key: bytes) -> None:
        """A fresh nonce every time.

        Reusing a nonce with the same key in GCM is the one mistake that breaks
        it outright, so the visible symptom -- identical ciphertext -- is worth
        an assertion of its own.
        """
        plain = _write(tmp_path / "plain.bin", b"same input" * 1000)
        first = tmp_path / "one.enc"
        second = tmp_path / "two.enc"
        encrypt_file(plain, first, key)
        encrypt_file(plain, second, key)

        assert first.read_bytes() != second.read_bytes()


class TestItRefusesWhatItShould:
    def test_the_wrong_passphrase_does_not_open_it(
        self, tmp_path: Path, key: bytes, params: EncryptionParams
    ) -> None:
        plain = _write(tmp_path / "plain.bin", b"private" * 500)
        sealed = tmp_path / "sealed.enc"
        encrypt_file(plain, sealed, key)
        wrong = derive_key("not the passphrase", params)

        with pytest.raises(WrongPassphrase):
            decrypt_file(sealed, tmp_path / "opened.bin", wrong)

    def test_a_failed_decryption_leaves_nothing_behind(
        self, tmp_path: Path, key: bytes, params: EncryptionParams
    ) -> None:
        """Half a plaintext is worse than none, because it reads as content."""
        plain = _write(tmp_path / "plain.bin", b"private" * 500_000)
        sealed = tmp_path / "sealed.enc"
        encrypt_file(plain, sealed, key)
        wrong = derive_key("not the passphrase", params)
        opened = tmp_path / "opened.bin"

        with pytest.raises(WrongPassphrase):
            decrypt_file(sealed, opened, wrong)

        assert not opened.exists()
        assert not list(tmp_path.glob("*.ferry-tmp"))

    def test_a_file_cut_mid_frame_is_refused(self, tmp_path: Path, key: bytes) -> None:
        """The easy half: a partial frame fails its own tag."""
        data = bytes(range(256)) * (CHUNK_BYTES // 256) * 3
        plain = _write(tmp_path / "plain.bin", data)
        sealed = tmp_path / "sealed.enc"
        encrypt_file(plain, sealed, key)

        body = sealed.read_bytes()
        _write(sealed, body[: len(body) // 2])

        with pytest.raises(WrongPassphrase):
            decrypt_file(sealed, tmp_path / "opened.bin", key)

    def test_a_file_cut_exactly_at_a_frame_boundary_is_refused(
        self, tmp_path: Path, key: bytes
    ) -> None:
        """The hard half, and the reason a frame says whether it is the last.

        Cutting mid-frame is caught by that frame's own tag. Cutting **between**
        frames leaves a file where every remaining frame is intact, so without
        an authenticated end marker decryption simply stops at EOF and hands
        back a conversation missing its ending, with nothing to say so.

        Two frames of a three-frame file, cut on the boundary.
        """
        data = b"".join(bytes([index]) * CHUNK_BYTES for index in range(3))
        plain = _write(tmp_path / "plain.bin", data)
        sealed = tmp_path / "sealed.enc"
        encrypt_file(plain, sealed, key)

        boundary = _HEADER + 2 * _FRAME
        body = sealed.read_bytes()
        assert len(body) > boundary, "the fixture must have three frames"
        _write(sealed, body[:boundary])

        opened = tmp_path / "opened.bin"
        with pytest.raises(WrongPassphrase):
            decrypt_file(sealed, opened, key)
        assert not opened.exists(), "two thirds of a conversation must not be left behind"

    def test_an_altered_byte_is_caught(self, tmp_path: Path, key: bytes) -> None:
        plain = _write(tmp_path / "plain.bin", b"private" * 500)
        sealed = tmp_path / "sealed.enc"
        encrypt_file(plain, sealed, key)

        body = bytearray(sealed.read_bytes())
        body[-1] ^= 0xFF
        _write(sealed, bytes(body))

        with pytest.raises(WrongPassphrase):
            decrypt_file(sealed, tmp_path / "opened.bin", key)

    def test_a_reordered_frame_is_caught(self, tmp_path: Path, key: bytes) -> None:
        """Frames are bound to their position, not just to the file.

        Swapping two frames of a transcript would reorder a conversation while
        every individual frame still carried a valid tag. What defeats it is
        that the **nonce** contains the frame number, so a frame moved to a
        different position is decrypted with the wrong nonce and fails.
        """
        data = b"".join(bytes([index]) * CHUNK_BYTES for index in range(3))
        plain = _write(tmp_path / "plain.bin", data)
        sealed = tmp_path / "sealed.enc"
        encrypt_file(plain, sealed, key)

        body = bytearray(sealed.read_bytes())
        first = bytes(body[_HEADER : _HEADER + _FRAME])
        second = bytes(body[_HEADER + _FRAME : _HEADER + 2 * _FRAME])
        body[_HEADER : _HEADER + _FRAME] = second
        body[_HEADER + _FRAME : _HEADER + 2 * _FRAME] = first
        _write(sealed, bytes(body))

        with pytest.raises(WrongPassphrase):
            decrypt_file(sealed, tmp_path / "opened.bin", key)

    def test_a_file_that_was_never_encrypted_is_refused_clearly(
        self, tmp_path: Path, key: bytes
    ) -> None:
        plain = _write(tmp_path / "plain.bin", b"just a file")

        with pytest.raises(WrongPassphrase, match="not an encrypted Ferry file"):
            decrypt_file(plain, tmp_path / "opened.bin", key)


class TestKeyDerivation:
    def test_every_bundle_gets_its_own_salt(self) -> None:
        """So one passphrase used twice does not produce one key."""
        assert new_params().salt != new_params().salt

    def test_the_same_passphrase_and_salt_give_the_same_key(
        self, params: EncryptionParams, key: bytes
    ) -> None:
        assert derive_key(PASSPHRASE, params) == key

    def test_the_same_passphrase_with_a_different_salt_gives_a_different_key(
        self, key: bytes
    ) -> None:
        assert derive_key(PASSPHRASE, new_params()) != key

    def test_the_key_is_256_bits(self, key: bytes) -> None:
        assert len(key) == 32

    def test_an_unreadable_salt_is_reported(self) -> None:
        with pytest.raises(ValueError, match="salt is not readable"):
            derive_key(PASSPHRASE, EncryptionParams(salt="not base64 !!"))


class TestParamsSurviveTheManifest:
    def test_they_round_trip(self, params: EncryptionParams) -> None:
        assert EncryptionParams.from_dict(dict(params.as_dict())) == params

    def test_an_algorithm_this_version_cannot_open_is_refused_in_words(self) -> None:
        """Not by producing rubbish.

        A bundle written by a later Ferry with a different cipher must say so,
        because the user's copy of that bundle is still perfectly good and the
        answer is to upgrade rather than to panic.
        """
        raw = {"salt": base64.b64encode(b"0123456789abcdef").decode(), "algorithm": "ChaCha20"}

        with pytest.raises(ValueError, match="cannot open"):
            EncryptionParams.from_dict(raw)

    def test_a_manifest_with_no_salt_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no salt"):
            EncryptionParams.from_dict({"algorithm": "AES-256-GCM", "kdf": "scrypt"})

    def test_nonsense_cost_parameters_fall_back_rather_than_raising(self) -> None:
        """The parameters exist so the cost can be raised later.

        A manifest edited into nonsense should try the defaults rather than
        refuse outright -- the salt is what actually matters, and it is checked.
        """
        raw = dict(new_params().as_dict())
        raw["n"] = "lots"
        raw["r"] = -1

        recovered = EncryptionParams.from_dict(raw)

        assert recovered.n > 1
        assert recovered.r > 0
