"""Bundle creation, reading, packing and validation, per PLAN.md §3.3.

A bundle is a directory while it is being built, and a zip once packed:

    ferry-bundle-<timestamp>.zip
    manifest.json
    conversations/<uuid>.json
    attachments/<conversation-uuid>/<attachment-uuid>.<ext>
    source_raw/<uuid>.bin

Writes use the atomic tmp+rename pattern required by PLAN.md §6.3 so an
interrupted or disk-full write cannot leave a half-written file in place.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from ferry.core.manifest import Manifest
from ferry.ucs import Attachment, Conversation

MANIFEST_NAME = "manifest.json"
CONVERSATIONS_DIR = "conversations"
ATTACHMENTS_DIR = "attachments"
SOURCE_RAW_DIR = "source_raw"


@dataclass(frozen=True)
class Removal:
    """What a delete took away.

    Returned rather than printed so the caller can say it in its own words, and
    so a test can assert on the numbers instead of on a sentence.
    """

    conversations: int = 0
    files: int = 0
    bytes_freed: int = 0
    backup: Path | None = None
    """Where the copy went, when one was taken. ``None`` means the caller asked
    for no backup -- never that one was attempted and failed."""


def _tree_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    return sorted(child for child in path.rglob("*") if child.is_file())


class BundleError(Exception):
    """Raised when a bundle cannot be created, read, or written."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_conversation(dest: Path, conversation: Conversation) -> None:
    """Write one conversation as JSON without ever holding the document in memory.

    Serialising a conversation in one call is the single largest thing Ferry
    does. On a 51 MB Codex rollout, ``model_dump_json(indent=2)`` peaked at
    **158 MB** to produce 19 MB of JSON — several times the size of the model
    itself, because the document exists as Python objects *and* as a string
    before a byte reaches the disk. That term, not the parsing, is what decided
    how large a transcript Ferry could handle.

    Messages are written straight to the file one at a time instead, so nothing
    larger than a single message is ever held: **8 MB peak for the same
    conversation, a 20x reduction.** The envelope still goes through pydantic,
    so a field added to :class:`Conversation` appears here without anyone
    remembering to update this function.

    Messages land one per line rather than pretty-printed across many. That is
    incidental but welcome: a 19 MB conversation diffs far better this way.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")

    # The same model with its messages removed: every other field, formatted by
    # pydantic exactly as it always was.
    envelope = json.loads(conversation.model_copy(update={"messages": []}).model_dump_json())
    envelope.pop("messages", None)
    head = json.dumps(envelope, indent=2, ensure_ascii=False)
    opening = head[: head.rindex("}")].rstrip().rstrip(",")
    if envelope:
        opening += ","

    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(f'{opening}\n  "messages": [\n')
        for index, message in enumerate(conversation.messages):
            if index:
                fh.write(",\n")
            fh.write("    " + message.model_dump_json())
        fh.write("\n  ]\n}\n")
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(dest)


def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(dest)


class Bundle:
    """A Ferry bundle backed by a directory on disk."""

    def __init__(self, root: Path, manifest: Manifest) -> None:
        self.root = root
        self.manifest = manifest

    # ---------- construction ----------

    @classmethod
    def create(cls, root: Path, manifest: Manifest, *, force: bool = False) -> Bundle:
        """Create a new bundle directory. Refuses to clobber an existing one without force."""
        if root.exists() and any(root.iterdir()):
            if not force:
                raise BundleError(
                    f"{root} already exists and is not empty (pass force=True to reuse)"
                )
        root.mkdir(parents=True, exist_ok=True)
        (root / CONVERSATIONS_DIR).mkdir(exist_ok=True)
        bundle = cls(root, manifest)
        bundle._write_manifest()
        return bundle

    @classmethod
    def open(cls, root: Path) -> Bundle:
        """Open an existing bundle directory."""
        manifest_path = root / MANIFEST_NAME
        if not manifest_path.is_file():
            raise BundleError(f"no {MANIFEST_NAME} in {root} - not a bundle")
        try:
            manifest = Manifest.model_validate_json(manifest_path.read_bytes())
        except ValidationError as exc:
            raise BundleError(f"{MANIFEST_NAME} is not a valid manifest: {exc}") from exc
        return cls(root, manifest)

    # ---------- writing ----------

    def _write_manifest(self) -> None:
        _atomic_write_bytes(
            self.root / MANIFEST_NAME,
            self.manifest.model_dump_json(indent=2).encode("utf-8"),
        )

    def conversation_path(self, conversation_id: UUID) -> Path:
        return self.root / CONVERSATIONS_DIR / f"{conversation_id}.json"

    def has_conversation(self, conversation_id: UUID) -> bool:
        """Used by adapters to skip already-written files when resuming an export."""
        return self.conversation_path(conversation_id).is_file()

    def add_conversation(self, conversation: Conversation) -> Path:
        dest = self.conversation_path(conversation.id)
        _write_conversation(dest, conversation)
        if conversation.source_tool not in self.manifest.tools_included:
            self.manifest.tools_included.append(conversation.source_tool)
        self.manifest.conversation_count = len(self.list_conversations())
        self._write_manifest()
        return dest

    def add_attachment(self, conversation_id: UUID, source: Path, attachment: Attachment) -> Path:
        """Copy an attachment into the bundle and verify it landed intact.

        The caller supplies the Attachment record (it belongs to the Conversation);
        this writes the bytes to the bundle_path that record points at.
        """
        if not source.is_file():
            raise BundleError(f"attachment source does not exist: {source}")
        dest = self._attachment_dest(conversation_id, attachment)
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.copyfile(source, tmp)
        tmp.replace(dest)
        return self._verify_attachment(dest, attachment)

    def add_attachment_bytes(
        self, conversation_id: UUID, data: bytes, attachment: Attachment
    ) -> Path:
        """Write an attachment that has no source file.

        Some tools store images inline inside the transcript rather than as
        files on disk -- Claude Code encodes them as base64 in the message. The
        bytes still belong in the bundle as a real file, so they can be
        checksummed and inspected like any other attachment.
        """
        dest = self._attachment_dest(conversation_id, attachment)
        _atomic_write_bytes(dest, data)
        return self._verify_attachment(dest, attachment)

    def _attachment_dest(self, conversation_id: UUID, attachment: Attachment) -> Path:
        expected_dir = f"{ATTACHMENTS_DIR}/{conversation_id}/"
        if not attachment.bundle_path.startswith(expected_dir):
            raise BundleError(
                f"attachment bundle_path {attachment.bundle_path!r} "
                f"does not sit under {expected_dir!r}"
            )
        dest = self._resolve_inside(attachment.bundle_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        return dest

    def _verify_attachment(self, dest: Path, attachment: Attachment) -> Path:
        actual = sha256_file(dest)
        if actual != attachment.sha256:
            raise BundleError(
                f"attachment {attachment.id} checksum mismatch after copy: "
                f"record says {attachment.sha256}, bytes hash to {actual}"
            )
        return dest

    # ---------- source_raw ----------

    def source_raw_path(self, conversation_id: UUID) -> Path:
        """Where this conversation's original-format bytes live (PLAN.md 3.3)."""
        return self.root / SOURCE_RAW_DIR / f"{conversation_id}.bin"

    def has_source_raw(self, conversation_id: UUID) -> bool:
        return self.source_raw_path(conversation_id).is_file()

    def add_source_raw(self, conversation_id: UUID, source: Path) -> Path:
        """Copy a conversation's original file in verbatim.

        Opt-in per PLAN.md 3.3 and the reason a same-tool re-import can be
        byte-perfect: UCS is a lossy common denominator by construction, and
        this is the copy that does not go through it.
        """
        if not source.is_file():
            raise BundleError(f"source_raw source does not exist: {source}")
        dest = self.source_raw_path(conversation_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.copyfile(source, tmp)
        tmp.replace(dest)
        return dest

    def source_raw_sidecar_dir(self, conversation_id: UUID) -> Path:
        """Companion files the original format kept beside the conversation.

        Claude Code spills large tool outputs to separate ``.txt`` files and
        leaves an absolute path to them in the transcript. They are neither
        messages nor user attachments -- they are part of the original format's
        bytes, so they sit under ``source_raw`` with it.
        """
        return self.root / SOURCE_RAW_DIR / str(conversation_id)

    def add_source_raw_sidecar(self, conversation_id: UUID, source: Path, relative: str) -> Path:
        if not source.is_file():
            raise BundleError(f"sidecar source does not exist: {source}")
        dest = self._resolve_inside(
            f"{SOURCE_RAW_DIR}/{conversation_id}/{relative}".replace("\\", "/")
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.copyfile(source, tmp)
        tmp.replace(dest)
        return dest

    def list_source_raw_sidecars(self, conversation_id: UUID) -> list[Path]:
        directory = self.source_raw_sidecar_dir(conversation_id)
        if not directory.is_dir():
            return []
        return sorted(p for p in directory.rglob("*") if p.is_file())

    # ---------- deleting ----------

    def conversation_files(self, conversation_id: UUID) -> list[Path]:
        """Every file that belongs to one conversation.

        **A conversation is not one file.** Removing the UCS document and
        leaving ``attachments/<uuid>/`` and ``source_raw/<uuid>.bin`` behind
        produces a bundle that still validates while carrying orphaned
        megabytes -- for an Antigravity conversation, the original database,
        which is most of its size.
        """
        return [
            *_tree_files(self.conversation_path(conversation_id)),
            *_tree_files(self.root / ATTACHMENTS_DIR / str(conversation_id)),
            *_tree_files(self.source_raw_path(conversation_id)),
            *_tree_files(self.source_raw_sidecar_dir(conversation_id)),
        ]

    def delete_conversation(self, conversation_id: UUID, *, backup: bool = True) -> Removal:
        """Remove one conversation and everything belonging to it.

        Args:
            conversation_id: Which one.
            backup: Copy it into ``~/.ferry/backups`` first. On by default, and
                the reasoning is not the same as for an import. An import
                overwrites something that also exists in the source tool; a
                bundle **is** the backup, so deleting from one is the only
                operation in Ferry after which the data is simply gone.

        Returns:
            What was removed.

        Raises:
            BundleError: If the conversation is not in this bundle, or if a
                backup was asked for and could not be taken. **The delete does
                not proceed in that case** -- a backup that failed is a reason
                to stop, not a step to skip.
        """
        if not self.has_conversation(conversation_id):
            raise BundleError(f"conversation {conversation_id} not in bundle")

        doomed = self.conversation_files(conversation_id)
        freed = sum(_size_of(path) for path in doomed)

        stored: Path | None = None
        if backup:
            from ferry.core.backup import back_up

            label = f"bundle-{self.root.name}"
            try:
                for path in doomed:
                    stored = back_up(path, label)
            except OSError as exc:
                raise BundleError(
                    f"could not back up {conversation_id} before deleting it, "
                    f"so nothing was removed: {exc}"
                ) from exc

        # The manifest goes last. Until it is rewritten the bundle still claims
        # this conversation, which `validate()` reports as a count mismatch --
        # a visible, repairable state. The alternative order leaves a bundle
        # that looks correct while the files are already gone.
        for path in doomed:
            path.unlink(missing_ok=True)
        for directory in (
            self.root / ATTACHMENTS_DIR / str(conversation_id),
            self.source_raw_sidecar_dir(conversation_id),
        ):
            _remove_empty_tree(directory)

        remaining = [self.load_tool(cid) for cid in self.list_conversations()]
        self.manifest.conversation_count = len(remaining)
        self.manifest.tools_included = [
            tool for tool in self.manifest.tools_included if tool in set(remaining)
        ]
        self._write_manifest()

        return Removal(
            conversations=1,
            files=len(doomed),
            bytes_freed=freed,
            backup=stored.parent if stored else None,
        )

    def load_tool(self, conversation_id: UUID) -> str | None:
        """The tool a conversation came from, without loading the whole thing.

        Used when rewriting ``tools_included`` after a delete. Loading every
        remaining conversation through the models to answer one question would
        make deleting from a 121 MB bundle cost as much as importing it.
        """
        try:
            raw = json.loads(self.conversation_path(conversation_id).read_bytes())
        except (OSError, ValueError):
            return None
        tool = raw.get("source_tool") if isinstance(raw, dict) else None
        return tool if isinstance(tool, str) else None

    # ---------- reading ----------

    def list_conversations(self) -> list[UUID]:
        conv_dir = self.root / CONVERSATIONS_DIR
        if not conv_dir.is_dir():
            return []
        ids: list[UUID] = []
        for path in sorted(conv_dir.glob("*.json")):
            try:
                ids.append(UUID(path.stem))
            except ValueError:
                continue
        return ids

    def load_conversation(self, conversation_id: UUID) -> Conversation:
        path = self.conversation_path(conversation_id)
        if not path.is_file():
            raise BundleError(f"conversation {conversation_id} not in bundle")
        try:
            return Conversation.model_validate_json(path.read_bytes())
        except ValidationError as exc:
            raise BundleError(f"conversation {conversation_id} is not valid UCS: {exc}") from exc

    # ---------- validation ----------

    def validate(self) -> list[str]:
        """Check the bundle end to end. Returns a list of problems; empty means valid.

        Returns problems rather than raising so the CLI can show every fault at once
        instead of one per run.
        """
        problems: list[str] = []

        manifest_path = self.root / MANIFEST_NAME
        if not manifest_path.is_file():
            return [f"missing {MANIFEST_NAME}"]

        conversation_ids = self.list_conversations()
        if self.manifest.conversation_count != len(conversation_ids):
            problems.append(
                f"manifest says {self.manifest.conversation_count} conversations, "
                f"found {len(conversation_ids)}"
            )

        for path in sorted((self.root / CONVERSATIONS_DIR).glob("*.json")):
            try:
                UUID(path.stem)
            except ValueError:
                problems.append(f"{path.name}: filename is not a UUID")
                continue

        for conversation_id in conversation_ids:
            try:
                conversation = self.load_conversation(conversation_id)
            except BundleError as exc:
                problems.append(str(exc))
                continue

            if conversation.id != conversation_id:
                problems.append(
                    f"{conversation_id}.json contains id {conversation.id} - "
                    "filename and id disagree"
                )
            if conversation.source_tool not in self.manifest.tools_included:
                problems.append(
                    f"{conversation_id}: source_tool {conversation.source_tool!r} "
                    "not listed in manifest.tools_included"
                )
            problems.extend(self._validate_attachments(conversation))

        return problems

    def _validate_attachments(self, conversation: Conversation) -> list[str]:
        problems: list[str] = []
        for attachment in conversation.attachments:
            try:
                path = self._resolve_inside(attachment.bundle_path)
            except BundleError as exc:
                problems.append(f"{conversation.id}: {exc}")
                continue
            if not path.is_file():
                problems.append(
                    f"{conversation.id}: attachment {attachment.id} "
                    f"missing at {attachment.bundle_path}"
                )
                continue
            actual = sha256_file(path)
            if actual != attachment.sha256:
                problems.append(
                    f"{conversation.id}: attachment {attachment.id} checksum mismatch "
                    f"(recorded {attachment.sha256}, actual {actual})"
                )
        return problems

    def _resolve_inside(self, relative: str) -> Path:
        """Resolve a bundle-relative path, refusing anything that escapes the bundle root."""
        root = self.root.resolve()
        candidate = (root / relative).resolve()
        if candidate != root and root not in candidate.parents:
            raise BundleError(f"path {relative!r} escapes the bundle root")
        return candidate

    # ---------- packing ----------

    def pack(self, dest: Path, *, force: bool = False) -> Path:
        """Zip the bundle directory. Refuses to overwrite without force (PLAN.md §6.1)."""
        if dest.exists() and not force:
            raise BundleError(f"{dest} already exists (pass force=True to overwrite)")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(self.root.rglob("*")):
                if path.is_file() and not path.name.endswith(".tmp"):
                    zf.write(path, path.relative_to(self.root).as_posix())
        tmp.replace(dest)
        return dest

    @classmethod
    def unpack(cls, archive: Path, dest_root: Path) -> Bundle:
        """Extract a packed bundle and open it.

        Rejects absolute paths and traversal entries rather than trusting the archive.
        """
        if not zipfile.is_zipfile(archive):
            raise BundleError(f"{archive} is not a zip file")
        dest_root.mkdir(parents=True, exist_ok=True)
        resolved_root = dest_root.resolve()
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                target = (resolved_root / name).resolve()
                if resolved_root not in target.parents and target != resolved_root:
                    raise BundleError(f"refusing to extract {name!r}: escapes destination")
            zf.extractall(resolved_root)
        return cls.open(dest_root)


def json_roundtrip_equal(a: Conversation, b: Conversation) -> bool:
    """True when two conversations serialise identically. Used by round-trip tests."""
    return bool(json.loads(a.model_dump_json()) == json.loads(b.model_dump_json()))


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _remove_empty_tree(directory: Path) -> None:
    """Drop a directory once nothing is left in it.

    Only when empty. A directory still holding files belongs to something this
    delete did not cover, and removing it would take that with it.
    """
    if not directory.is_dir():
        return
    for child in sorted(directory.rglob("*"), reverse=True):
        if child.is_dir() and not any(child.iterdir()):
            child.rmdir()
    if not any(directory.iterdir()):
        directory.rmdir()


def delete_bundle(root: Path, *, backup: bool = False) -> Removal:
    """Remove a whole bundle directory.

    **Refuses anything that is not a bundle.** This is the only function in
    Ferry that deletes a directory tree it did not create, and the check that
    ``manifest.json`` is present is what stands between a mistyped path and
    someone's Documents folder.

    Args:
        root: The bundle directory.
        backup: Copy every file into ``~/.ferry/backups`` first. **Off by
            default here**, unlike deleting a single conversation: a whole
            bundle is routinely gigabytes, and copying it to delete it is not
            a backup so much as a rename the user did not ask for. The caller
            asks.

    Raises:
        BundleError: If ``root`` is not a bundle, or a requested backup failed.
    """
    if not (root / MANIFEST_NAME).is_file():
        raise BundleError(f"{root} is not a bundle - refusing to delete it")

    files = _tree_files(root)
    freed = sum(_size_of(path) for path in files)
    conversations = len(Bundle.open(root).list_conversations())

    stored: Path | None = None
    if backup:
        from ferry.core.backup import back_up

        label = f"bundle-{root.name}"
        try:
            for path in files:
                stored = back_up(path, label)
        except OSError as exc:
            raise BundleError(
                f"could not back {root.name} up before deleting it, so nothing was removed: {exc}"
            ) from exc

    shutil.rmtree(root)
    return Removal(
        conversations=conversations,
        files=len(files),
        bytes_freed=freed,
        backup=stored.parent if stored else None,
    )
