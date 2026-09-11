"""Where Antigravity keeps its data, and how to open it without disturbing it.

Verified against Antigravity 2.8.1 on Windows. The layout is::

    ~/.gemini/antigravity/conversations/<uuid>.db      one database per conversation
    ~/.gemini/antigravity/brain/<uuid>/                working directory for that conversation
        .user_uploaded/media*.png                      images the user attached
        .system_generated/logs/transcript.jsonl        the plain-JSON cross-check
    ~/.gemini/config/projects/<project-uuid>.json      folder a conversation belongs to

**No platform branch.** Unlike VS Code, Antigravity puts everything under
``~/.gemini`` on every operating system, so there is nothing here to get wrong
per platform -- which is worth stating, because the same assumption was wrong
for two of the other three adapters.

The plan recorded that v1.x used ``antigravity/`` and v2.x used
``antigravity-ide/``. The probe found 2.8.1 using ``antigravity/``, so that
claim is unsupported and no version-detection logic is built on it.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

__all__ = [
    "DATA_DIR_ENV",
    "INSTALL_DIR_ENV",
    "Project",
    "antigravity_version",
    "attachment_files",
    "brain_dir",
    "conversation_databases",
    "conversation_id_of",
    "conversations_dir",
    "data_dir",
    "open_readonly",
    "projects",
    "projects_dir",
    "transcript_path",
]

DATA_DIR_ENV = "FERRY_ANTIGRAVITY_DIR"
"""Points Ferry at a different ``~/.gemini/antigravity``. Ferry's own variable."""

INSTALL_DIR_ENV = "FERRY_ANTIGRAVITY_INSTALL"
"""Points Ferry at a different Antigravity installation directory."""

ATTACHMENT_GLOB = "media*"
"""Matches both spellings the uploader produces.

The plan records ``media_<epoch_ms>.png``. Real directories hold
``media_1786163647832.png`` **and** ``media__1786017081245.png``, with two
underscores, in roughly equal numbers. Globbing on ``media_`` would have
silently dropped every attachment in the conversations using the other
spelling.
"""


def data_dir(env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """The Antigravity data root."""
    environ = os.environ if env is None else env
    override = environ.get(DATA_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".gemini" / "antigravity"


def conversations_dir(env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """The directory holding one SQLite database per conversation."""
    return data_dir(env) / "conversations"


def brain_dir(conversation_id: str, env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """One conversation's working directory.

    Also a git repository, which Ferry neither reads nor reproduces: the
    history it holds is of the files the agent edited, not of the conversation.
    """
    return data_dir(env) / "brain" / conversation_id


def projects_dir(env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """Where the folder-to-project mapping lives.

    Beside ``antigravity/``, not inside it, so it is not derived from
    :func:`data_dir` -- pointing ``FERRY_ANTIGRAVITY_DIR`` at a scratch copy
    must not silently redirect this to a directory that does not exist.
    """
    environ = os.environ if env is None else env
    override = environ.get(DATA_DIR_ENV)
    if override:
        return Path(override).parent / "config" / "projects"
    return Path.home() / ".gemini" / "config" / "projects"


def skills_dir(env: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """Where Antigravity looks for a person's own skills: ``~/.gemini/config/skills``.

    From Antigravity's own documentation, checked 2026-09-12. It sits beside
    ``antigravity/`` like :func:`projects_dir`, and is redirected the same way.
    """
    environ = os.environ if env is None else env
    override = environ.get(DATA_DIR_ENV)
    if override:
        return Path(override).parent / "config" / "skills"
    return Path.home() / ".gemini" / "config" / "skills"


def conversation_databases(
    env: os._Environ[str] | dict[str, str] | None = None,
) -> list[Path]:
    """Every conversation database, sorted so an interrupted export resumes predictably.

    ``*.db`` only. Each database is accompanied by ``-wal`` and ``-shm``
    sidecars that are part of the same database, not separate ones.
    """
    root = conversations_dir(env)
    if not root.is_dir():
        return []
    return sorted(root.glob("*.db"))


def conversation_id_of(path: Path) -> UUID | None:
    """The conversation's id, or ``None`` if the filename is not one."""
    try:
        return UUID(path.stem)
    except ValueError:
        return None


def transcript_path(
    conversation_id: str, env: os._Environ[str] | dict[str, str] | None = None
) -> Path:
    """The plain-JSON transcript for a conversation.

    A cross-check, never a source: 9.8% of its entries carry
    ``truncated_fields``. See ``docs/FORMATS.md``.
    """
    return brain_dir(conversation_id, env) / ".system_generated" / "logs" / "transcript.jsonl"


def attachment_files(
    conversation_id: str, env: os._Environ[str] | dict[str, str] | None = None
) -> list[Path]:
    """Images the user attached to a conversation, oldest first.

    The filename carries an epoch in milliseconds, but sorting is by name
    rather than by parsed time: the two spellings pad differently and a
    filename Ferry cannot parse must still be carried, not dropped.
    """
    directory = brain_dir(conversation_id, env) / ".user_uploaded"
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob(ATTACHMENT_GLOB) if path.is_file())


class Project:
    """A folder Antigravity groups conversations under.

    ``folder`` is the absolute path on the machine that wrote it, so it is one
    of the things an import has to remap. It is optional: the built-in
    ``outside-of-project`` record has a name and no folder at all.
    """

    __slots__ = ("id", "name", "folder")

    def __init__(self, id: str, name: str, folder: str | None) -> None:
        self.id = id
        self.name = name
        self.folder = folder

    def __repr__(self) -> str:
        return f"Project(id={self.id!r}, name={self.name!r}, folder={self.folder!r})"


def _folder_of(document: dict[str, object]) -> str | None:
    """The folder URI in a project record, whichever shape it is written in.

    Two shapes exist on real data -- ``{"folderUri": ...}`` and
    ``{"gitFolder": {"folderUri": ...}}`` -- and a project whose folder is
    under git uses the second. Reading only the first would report the
    conversation as belonging to no folder, which is exactly the case where
    the folder matters most.
    """
    resources = document.get("projectResources")
    if not isinstance(resources, dict):
        return None
    entries = resources.get("resources")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for candidate in (entry, entry.get("gitFolder")):
            if isinstance(candidate, dict):
                uri = candidate.get("folderUri")
                if isinstance(uri, str) and uri:
                    return uri
    return None


def projects(env: os._Environ[str] | dict[str, str] | None = None) -> dict[str, Project]:
    """Every project, keyed by id.

    A conversation names its project in ``trajectory_metadata_blob`` field 18
    -- true for 6 of 6 conversations on the probe machine -- which is how an
    imported conversation finds the folder it belongs to.
    """
    found: dict[str, Project] = {}
    root = projects_dir(env)
    if not root.is_dir():
        return found
    for path in sorted(root.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        identifier = document.get("id")
        if not isinstance(identifier, str) or not identifier:
            continue
        name = document.get("name")
        found[identifier] = Project(
            identifier,
            name if isinstance(name, str) else identifier,
            _folder_of(document),
        )
    return found


def _asar_package(archive: Path) -> dict[str, object] | None:
    """``package.json`` out of an Electron ``.asar``, without unpacking it.

    The archive begins with a 16-byte prelude: the second word is the size of
    the header pickle and the fourth is the length of the JSON inside it. Data
    starts at ``8 + pickle size`` -- not after the JSON, which is the obvious
    reading and produces an offset a few bytes wrong, so entries decode as
    binary rather than failing outright.
    """
    try:
        with archive.open("rb") as handle:
            prelude = handle.read(16)
            if len(prelude) < 16:
                return None
            pickle_size, json_size = (
                struct.unpack("<I", prelude[4:8])[0],
                struct.unpack("<I", prelude[12:16])[0],
            )
            if json_size > 32 * 1024 * 1024:
                return None
            header = json.loads(handle.read(json_size).rstrip(b"\x00").decode("utf-8"))
            entry = header.get("files", {}).get("package.json")
            if not isinstance(entry, dict):
                return None
            handle.seek(8 + pickle_size + int(entry["offset"]))
            document = json.loads(handle.read(int(entry["size"])).decode("utf-8"))
    except (OSError, ValueError, KeyError, TypeError, struct.error, UnicodeDecodeError):
        return None
    return document if isinstance(document, dict) else None


def install_dir(env: os._Environ[str] | dict[str, str] | None = None) -> Path | None:
    """The Antigravity installation, or ``None`` if it is not where it usually is."""
    environ = os.environ if env is None else env
    override = environ.get(INSTALL_DIR_ENV)
    if override:
        candidate = Path(override)
        return candidate if candidate.is_dir() else None

    candidates: list[Path] = []
    if sys.platform == "win32":
        local = environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "Programs" / "antigravity")
        candidates.append(Path("C:/Program Files/Antigravity"))
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Antigravity.app/Contents"))
        candidates.append(Path.home() / "Applications" / "Antigravity.app" / "Contents")
    else:
        candidates.append(Path("/usr/share/antigravity"))
        candidates.append(Path("/opt/Antigravity"))

    return next((path for path in candidates if path.is_dir()), None)


def antigravity_version(env: os._Environ[str] | dict[str, str] | None = None) -> str | None:
    """The installed version, read from the packaged ``package.json``.

    Antigravity writes no version anywhere in its data directory -- unlike VS
    Code, which records one in its state database -- so the only honest source
    is the install itself. ``None`` when the install is not found or the
    archive does not read, which is reported as unknown rather than assumed to
    match the version this adapter was verified against.
    """
    install = install_dir(env)
    if install is None:
        return None
    archive = install / "resources" / "app.asar"
    if not archive.is_file():
        return None
    package = _asar_package(archive)
    if package is None:
        return None
    version = package.get("version")
    return version if isinstance(version, str) and version else None


@contextmanager
def open_readonly(database: Path) -> Iterator[sqlite3.Connection]:
    """A read-only connection to a conversation database.

    Antigravity keeps these in WAL mode, and the newest steps of an open
    conversation live in the ``-wal`` file rather than the database. Two things
    follow, and both matter:

    * Opening with ``immutable=1`` would ignore the WAL and quietly return a
      stale conversation -- missing exactly the messages the user most likely
      wants.
    * Opening ``mode=ro`` reads the WAL, but needs the ``-shm`` file, and
      **fails rather than creating it** when it is absent. That failure is the
      good outcome: the alternative would be Ferry writing into the store it
      promised only to read.

    So the fallback is a copy. The database and both sidecars are copied to a
    temporary directory and opened there, where SQLite may create and recover
    whatever it likes without touching the user's data.
    """
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        connection.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.Error:
        with tempfile.TemporaryDirectory(prefix="ferry-antigravity-") as scratch:
            copy = Path(scratch) / database.name
            shutil.copy2(database, copy)
            for suffix in ("-wal", "-shm"):
                sidecar = database.with_name(database.name + suffix)
                if sidecar.is_file():
                    shutil.copy2(sidecar, copy.with_name(copy.name + suffix))
            connection = sqlite3.connect(copy)
            try:
                yield connection
            finally:
                connection.close()
            return
    try:
        yield connection
    finally:
        connection.close()
