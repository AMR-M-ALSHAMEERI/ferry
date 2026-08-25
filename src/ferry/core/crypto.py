"""Encrypting a stream of bytes, per PLAN.md §5 M7.

This module knows about files and keys and nothing about bundles. What gets
encrypted, and when, is :mod:`ferry.core.sealed`.

**Chunked, not one-shot.** ``AESGCM.encrypt`` takes the whole plaintext, and a
sealed bundle on the reference machine is 121 MB. Everything else in Ferry
streams for exactly that reason, so this does too: 1 MiB frames, each with its
own nonce and tag, and memory that stays flat however large the input.

Three properties the framing has to provide, because AES-GCM alone does not:

* **A frame cannot be moved.** Its number is part of its nonce, so a frame
  decrypted at the wrong position fails.
* **The end is authenticated.** Each frame says whether it is the last, and
  that flag is covered by the tag. Without it, cutting a file **between**
  frames leaves every remaining frame intact and decryption simply stops at
  EOF -- handing back two thirds of a conversation as though it were whole.
* **A failure leaves nothing.** Output goes to a temporary file and is renamed
  only once the last frame authenticates. Half a plaintext is worse than none,
  because it reads as content.

The framing is the only thing invented here. AES-256-GCM and scrypt both come
from ``cryptography``; Ferry writes no cryptographic primitive of its own.

**A lost passphrase is a lost bundle.** There is no recovery, no hint, no
escrow, and nothing in this module can be asked for one. Any interface offering
encryption has to say that in those words before it is used.
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

__all__ = [
    "ALGORITHM",
    "CHUNK_BYTES",
    "KDF",
    "MAGIC",
    "EncryptionParams",
    "WrongPassphrase",
    "decrypt_file",
    "derive_key",
    "encrypt_file",
    "is_encrypted",
    "new_params",
]

MAGIC: Final = b"FERRYENC"
"""Opens every encrypted file, so one can be recognised without the manifest.

A bundle whose manifest is lost or edited is still readable as *encrypted*
rather than as corrupt, which is the difference between "find your passphrase"
and "this file is ruined"."""

FORMAT_VERSION: Final = 1
ALGORITHM: Final = "AES-256-GCM"
KDF: Final = "scrypt"

CHUNK_BYTES: Final = 1 << 20
"""1 MiB of plaintext per frame.

Large enough that the 16-byte tag and 12-byte nonce per frame cost about
0.003% in size; small enough that memory stays flat on a 53 MB conversation."""

_TAG_BYTES: Final = 16
_NONCE_PREFIX_BYTES: Final = 4
_COUNTER_BYTES: Final = 8
_LENGTH_BYTES: Final = 4
_HEADER_BYTES: Final = len(MAGIC) + 2 + _NONCE_PREFIX_BYTES

# scrypt cost. n=2**16 with r=8 needs about 64 MB and takes a fraction of a
# second -- deliberately slow enough that guessing a weak passphrase is
# expensive, and light enough to run on a laptop that is also holding a bundle
# in memory. Recorded in the manifest rather than assumed, so raising it later
# leaves old bundles readable.
_SCRYPT_N: Final = 1 << 16
_SCRYPT_R: Final = 8
_SCRYPT_P: Final = 1
_KEY_BYTES: Final = 32
_SALT_BYTES: Final = 16


class WrongPassphrase(Exception):
    """The passphrase does not open this file.

    Raised on any authentication failure, which covers a mistyped passphrase
    and a tampered file alike. **The two are deliberately not distinguished**:
    telling an attacker which of the two happened is how a decryption oracle
    starts, and the user's next action is the same either way.
    """


@dataclass(frozen=True)
class EncryptionParams:
    """What the manifest records so a bundle can be opened again.

    Everything here is public. The salt is not a secret -- it exists so that
    two bundles with the same passphrase do not share a key, and so a
    precomputed table cannot cover both.
    """

    salt: str
    algorithm: str = ALGORITHM
    kdf: str = KDF
    n: int = _SCRYPT_N
    r: int = _SCRYPT_R
    p: int = _SCRYPT_P

    def as_dict(self) -> dict[str, str | int]:
        return {
            "salt": self.salt,
            "algorithm": self.algorithm,
            "kdf": self.kdf,
            "n": self.n,
            "r": self.r,
            "p": self.p,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> EncryptionParams:
        """Rebuild from a manifest, refusing anything this version cannot open.

        A bundle written by a future Ferry with a different algorithm must fail
        with a sentence rather than by producing rubbish.
        """
        algorithm = str(raw.get("algorithm", ALGORITHM))
        kdf = str(raw.get("kdf", KDF))
        if algorithm != ALGORITHM or kdf != KDF:
            raise ValueError(
                f"this bundle uses {algorithm} with {kdf}, which this version of Ferry "
                f"cannot open (it understands {ALGORITHM} with {KDF})"
            )
        salt = raw.get("salt")
        if not isinstance(salt, str) or not salt:
            raise ValueError("the bundle records no salt, so its key cannot be derived")
        return cls(
            salt=salt,
            algorithm=algorithm,
            kdf=kdf,
            n=_whole(raw.get("n"), _SCRYPT_N),
            r=_whole(raw.get("r"), _SCRYPT_R),
            p=_whole(raw.get("p"), _SCRYPT_P),
        )


def _whole(value: object, fallback: int) -> int:
    """One cost parameter from a manifest, or the default it was written with.

    A value that is not a positive whole number falls back rather than raising:
    the parameters are recorded so that *raising* the cost later leaves old
    bundles readable, and a manifest edited into nonsense should still try the
    defaults rather than refuse outright.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return fallback
    return value


def new_params() -> EncryptionParams:
    """Fresh parameters for a new bundle, with a random salt."""
    return EncryptionParams(salt=base64.b64encode(secrets.token_bytes(_SALT_BYTES)).decode("ascii"))


def derive_key(passphrase: str, params: EncryptionParams) -> bytes:
    """Turn a passphrase into a 32-byte key.

    Deliberately expensive. This is the only thing standing between a short
    passphrase and someone with the bundle, and it is run once per bundle
    rather than once per file.
    """
    try:
        salt = base64.b64decode(params.salt, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("the bundle's salt is not readable") from exc
    kdf = Scrypt(salt=salt, length=_KEY_BYTES, n=params.n, r=params.r, p=params.p)
    return kdf.derive(passphrase.encode("utf-8"))


def _nonce(prefix: bytes, counter: int) -> bytes:
    return prefix + counter.to_bytes(_COUNTER_BYTES, "big")


def _aad(header: bytes, counter: int, last: bool) -> bytes:
    """What each frame is authenticated against.

    The header binds the frame to this file's parameters, the counter to its
    position, and ``last`` to being the end. **Without the last flag, dropping
    the final frames produces a shorter file that still decrypts perfectly** --
    a truncated conversation that looks complete is exactly the failure Ferry
    exists to prevent.
    """
    return header + counter.to_bytes(_COUNTER_BYTES, "big") + (b"\x01" if last else b"\x00")


def is_encrypted(path: Path) -> bool:
    """Whether a file was written by :func:`encrypt_file`."""
    try:
        with path.open("rb") as handle:
            return handle.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def encrypt_file(source: Path, destination: Path, key: bytes) -> int:
    """Encrypt ``source`` into ``destination``. Returns the bytes written.

    Written to a temporary file and renamed, like every other write in Ferry,
    so an interrupted encryption cannot leave a file that opens and is wrong.
    """
    cipher = AESGCM(key)
    prefix = secrets.token_bytes(_NONCE_PREFIX_BYTES)
    header = MAGIC + bytes([FORMAT_VERSION, CHUNK_BYTES.bit_length() - 1]) + prefix

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".ferry-tmp")
    written = 0
    try:
        with source.open("rb") as reading, temporary.open("wb") as writing:
            writing.write(header)
            written += len(header)
            counter = 0
            chunk = reading.read(CHUNK_BYTES)
            while True:
                following = reading.read(CHUNK_BYTES)
                last = not following
                sealed = cipher.encrypt(_nonce(prefix, counter), chunk, _aad(header, counter, last))
                writing.write(len(sealed).to_bytes(_LENGTH_BYTES, "big"))
                writing.write(sealed)
                written += _LENGTH_BYTES + len(sealed)
                if last:
                    break
                chunk = following
                counter += 1
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return written


def _read_header(handle: BinaryIO, name: str) -> tuple[bytes, bytes]:
    header = handle.read(_HEADER_BYTES)
    if len(header) < _HEADER_BYTES or not header.startswith(MAGIC):
        raise WrongPassphrase(f"{name} is not an encrypted Ferry file")
    version = header[len(MAGIC)]
    if version != FORMAT_VERSION:
        raise WrongPassphrase(
            f"{name} was written in encrypted format {version}, "
            f"which this version of Ferry cannot read"
        )
    return header, header[len(MAGIC) + 2 :]


def decrypt_file(source: Path, destination: Path, key: bytes) -> int:
    """Decrypt ``source`` into ``destination``. Returns the bytes written.

    Raises:
        WrongPassphrase: If the key is wrong, or the file has been altered or
            truncated. **Nothing is left at ``destination``** when this
            happens -- a partial plaintext from a failed decryption is worse
            than no file, because it reads as a conversation.
    """
    cipher = AESGCM(key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".ferry-tmp")
    written = 0
    try:
        with source.open("rb") as reading, temporary.open("wb") as writing:
            header, prefix = _read_header(reading, source.name)
            counter = 0
            while True:
                size_bytes = reading.read(_LENGTH_BYTES)
                if not size_bytes:
                    # Ran out of frames without one claiming to be last.
                    raise WrongPassphrase(f"{source.name} is truncated")
                if len(size_bytes) < _LENGTH_BYTES:
                    raise WrongPassphrase(f"{source.name} is truncated")
                size = int.from_bytes(size_bytes, "big")
                if size < _TAG_BYTES or size > CHUNK_BYTES + _TAG_BYTES:
                    raise WrongPassphrase(f"{source.name} is not readable")
                sealed = reading.read(size)
                if len(sealed) < size:
                    raise WrongPassphrase(f"{source.name} is truncated")

                # Which frame this is -- last or not -- is authenticated, so it
                # is tried rather than trusted.
                for last in (False, True):
                    try:
                        plain = cipher.decrypt(
                            _nonce(prefix, counter), sealed, _aad(header, counter, last)
                        )
                    except InvalidTag:
                        continue
                    writing.write(plain)
                    written += len(plain)
                    break
                else:
                    raise WrongPassphrase(
                        f"{source.name} could not be opened - the passphrase is wrong, "
                        "or the file has been altered"
                    )
                if last:
                    break
                counter += 1
        temporary.replace(destination)
    except BaseException:
        # Only the temporary. ``destination`` is untouched until the rename, so
        # a failed decryption cannot damage a file that was already there.
        temporary.unlink(missing_ok=True)
        raise
    return written
