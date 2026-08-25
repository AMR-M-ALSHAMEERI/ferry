"""Turning a bundle into one encrypted file, and back again.

A **sealed bundle** is a single ``.ferry`` file: the packed bundle, encrypted
whole. Nothing about it is readable without the passphrase -- not the
conversation count, not which tools it came from, not the date it was made.

**Why sealing rather than encrypting each file as it is written.** Encrypting
in place would mean every read and write path in Ferry learning about keys:
four adapters, ``validate()``, ``inspect``, the attachment checksums. Nine
places, all of them stabilised over days of work, and a mistake in any of them
is *silent* -- files that look encrypted and can never be opened again. Sealing
puts encryption in one auditable place that either works or visibly does not.

**What that costs, stated plainly because the user is trusting it.** The
unencrypted bundle exists on disk while it is being made, and again in a
temporary folder while Ferry reads a sealed one. Ferry deletes both. On most
filesystems the blocks are not overwritten, so a forensic tool could recover
them until that space is reused. **Sealing protects a bundle you carry or
store; it does not protect the machine that made it.** Anyone who needs the
second thing needs full-disk encryption, which is not something a backup tool
can provide.

The temporary folder lives under ``~/.ferry`` rather than the system temp
directory: it is the user's own data, it should be somewhere they can find it
if Ferry dies mid-read, and it should not be handed to a directory the world
can often list.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ferry.core.bundle import MANIFEST_NAME, Bundle, BundleError
from ferry.core.crypto import (
    EncryptionParams,
    WrongPassphrase,
    decrypt_file,
    derive_key,
    encrypt_file,
    new_params,
)

__all__ = [
    "SEALED_MAGIC",
    "SEALED_SUFFIX",
    "SealedBundle",
    "is_sealed",
    "read_sealed_params",
    "opens_with",
    "seal_bundle",
    "unseal_bundle",
    "unsealed",
]

SEALED_SUFFIX: Final = ".ferry"
SEALED_MAGIC: Final = b"FERRYSLD"
_VERSION: Final = 1
_LENGTH_BYTES: Final = 2


@dataclass(frozen=True)
class SealedBundle:
    """What sealing produced."""

    path: Path
    bytes_written: int
    conversations: int


def is_sealed(path: Path) -> bool:
    """Whether ``path`` is a sealed bundle.

    Read from the file's own opening bytes rather than from its extension, so
    a renamed file is still recognised and a ``.ferry`` file that is something
    else is not mistaken for one.
    """
    try:
        with path.open("rb") as handle:
            return handle.read(len(SEALED_MAGIC)) == SEALED_MAGIC
    except OSError:
        return False


def _preamble(params: EncryptionParams) -> bytes:
    """The plaintext header carrying what is needed to derive the key.

    The salt and the scrypt cost cannot themselves be encrypted -- they are
    what the passphrase is turned into a key *with*. None of it is secret: the
    salt exists so that two bundles sharing a passphrase do not share a key.
    """
    body = json.dumps(params.as_dict(), sort_keys=True).encode("utf-8")
    return SEALED_MAGIC + bytes([_VERSION]) + len(body).to_bytes(_LENGTH_BYTES, "big") + body


def read_sealed_params(path: Path) -> EncryptionParams:
    """The key parameters of a sealed bundle, without opening it.

    Raises:
        BundleError: If this is not a sealed bundle, or was sealed by a version
            of Ferry this one cannot read.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(len(SEALED_MAGIC) + 1 + _LENGTH_BYTES)
            if len(head) < len(SEALED_MAGIC) + 1 + _LENGTH_BYTES or not head.startswith(
                SEALED_MAGIC
            ):
                raise BundleError(f"{path.name} is not a sealed Ferry bundle")
            version = head[len(SEALED_MAGIC)]
            if version != _VERSION:
                raise BundleError(
                    f"{path.name} was sealed in format {version}, "
                    "which this version of Ferry cannot open"
                )
            size = int.from_bytes(head[len(SEALED_MAGIC) + 1 :], "big")
            body = handle.read(size)
            if len(body) < size:
                raise BundleError(f"{path.name} is truncated")
    except OSError as exc:
        raise BundleError(f"cannot read {path}: {exc}") from exc

    try:
        raw = json.loads(body)
    except ValueError as exc:
        raise BundleError(f"{path.name} has an unreadable header") from exc
    if not isinstance(raw, dict):
        raise BundleError(f"{path.name} has an unreadable header")
    try:
        return EncryptionParams.from_dict(raw)
    except ValueError as exc:
        raise BundleError(str(exc)) from exc


def seal_bundle(root: Path, passphrase: str, destination: Path | None = None) -> SealedBundle:
    """Pack a bundle and encrypt it into one ``.ferry`` file.

    **Does not delete the original.** Removing someone's only copy of their
    conversations as a side effect of encrypting it is not this function's
    decision to make; the caller asks, after checking the sealed file opens.

    Args:
        root: The bundle directory.
        passphrase: What it will be opened with. There is no recovery.
        destination: Where to write. Defaults to ``<root>.ferry`` beside it.

    Raises:
        BundleError: If ``root`` is not a bundle, or the passphrase is empty.
    """
    if not (root / MANIFEST_NAME).is_file():
        raise BundleError(f"{root} is not a bundle")
    if not passphrase:
        raise BundleError("a sealed bundle needs a passphrase, and there is no recovery without it")

    bundle = Bundle.open(root)
    conversations = len(bundle.list_conversations())
    target = destination or root.with_name(root.name + SEALED_SUFFIX)

    params = new_params()
    key = derive_key(passphrase, params)

    # The zip is a temporary that exists only to be encrypted, so it is made
    # beside the destination rather than in the bundle -- an interrupted seal
    # must not leave a stray archive inside someone's backup.
    holder = Path(tempfile.mkdtemp(prefix="ferry-seal-", dir=target.parent))
    try:
        archive = bundle.pack(holder / "bundle.zip")
        staged = holder / "sealed"
        written = encrypt_file(archive, staged, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            handle.write(_preamble(params))
            with staged.open("rb") as sealed:
                shutil.copyfileobj(sealed, handle, length=1 << 20)
        written += len(_preamble(params))
    finally:
        shutil.rmtree(holder, ignore_errors=True)

    return SealedBundle(path=target, bytes_written=written, conversations=conversations)


def unseal_bundle(archive: Path, passphrase: str, destination: Path) -> Bundle:
    """Open a sealed bundle into ``destination`` and return it.

    Raises:
        BundleError: If this is not a sealed bundle.
        WrongPassphrase: If the passphrase is wrong or the file was altered.
    """
    params = read_sealed_params(archive)
    key = derive_key(passphrase, params)
    offset = len(_preamble(params))

    holder = Path(tempfile.mkdtemp(prefix="ferry-unseal-", dir=destination.parent))
    try:
        body = holder / "sealed"
        with archive.open("rb") as reading, body.open("wb") as writing:
            reading.seek(offset)
            shutil.copyfileobj(reading, writing, length=1 << 20)
        zipped = holder / "bundle.zip"
        decrypt_file(body, zipped, key)
        destination.mkdir(parents=True, exist_ok=True)
        return Bundle.unpack(zipped, destination)
    finally:
        shutil.rmtree(holder, ignore_errors=True)


@contextmanager
def unsealed(archive: Path, passphrase: str) -> Iterator[Bundle]:
    """Open a sealed bundle for as long as the block runs, then remove it.

    The unsealed copy goes under ``~/.ferry/open`` rather than the system
    temporary directory: it is the user's conversation history, and it belongs
    somewhere they can find it if Ferry is killed halfway through.

    **The copy is deleted on the way out, including when the block raises.**
    What deletion cannot promise is that the blocks are overwritten -- see the
    module docstring.
    """
    workspace = Path.home() / ".ferry" / "open"
    workspace.mkdir(parents=True, exist_ok=True)
    holder = Path(tempfile.mkdtemp(prefix="bundle-", dir=workspace))
    try:
        yield unseal_bundle(archive, passphrase, holder / "bundle")
    finally:
        shutil.rmtree(holder, ignore_errors=True)


def opens_with(archive: Path, passphrase: str) -> bool:
    """Whether ``passphrase`` opens ``archive``, without unpacking it.

    Used to confirm a freshly sealed bundle before offering to delete the
    original. Reads the whole file, which is the only honest way to answer:
    a passphrase that decrypts the first frame and fails on the last has not
    opened the bundle.
    """
    try:
        params = read_sealed_params(archive)
        key = derive_key(passphrase, params)
    except (BundleError, ValueError):
        return False

    holder = Path(tempfile.mkdtemp(prefix="ferry-check-", dir=archive.parent))
    try:
        body = holder / "sealed"
        with archive.open("rb") as reading, body.open("wb") as writing:
            reading.seek(len(_preamble(params)))
            shutil.copyfileobj(reading, writing, length=1 << 20)
        decrypt_file(body, holder / "out.zip", key)
    except (WrongPassphrase, OSError):
        return False
    finally:
        shutil.rmtree(holder, ignore_errors=True)
    return True
