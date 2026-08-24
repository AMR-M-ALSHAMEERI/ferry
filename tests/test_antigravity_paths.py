"""Finding Antigravity's data, and opening it without disturbing it."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import UUID

from ferry.adapters.antigravity import paths
from tests.antigravity_fixture import build_database

CONV = "aaaaaaaa-1111-4111-8111-111111111111"


def make_env(root: Path) -> dict[str, str]:
    return {paths.DATA_DIR_ENV: str(root / "antigravity")}


class TestLocations:
    def test_env_override_moves_the_data_root(self, tmp_path: Path) -> None:
        env = make_env(tmp_path)
        assert paths.data_dir(env) == tmp_path / "antigravity"
        assert paths.conversations_dir(env) == tmp_path / "antigravity" / "conversations"

    def test_projects_sit_beside_the_data_root_not_inside_it(self, tmp_path: Path) -> None:
        """``config/projects`` is a sibling of ``antigravity/``.

        Deriving it from the data root would put it inside, and an override
        pointed at a scratch copy would then silently look somewhere empty.
        """
        assert paths.projects_dir(make_env(tmp_path)) == tmp_path / "config" / "projects"

    def test_no_databases_when_the_directory_is_missing(self, tmp_path: Path) -> None:
        assert paths.conversation_databases(make_env(tmp_path)) == []

    def test_databases_are_sorted_and_exclude_sidecars(self, tmp_path: Path) -> None:
        directory = tmp_path / "antigravity" / "conversations"
        directory.mkdir(parents=True)
        for name in ("b.db", "a.db", "a.db-wal", "a.db-shm"):
            (directory / name).write_bytes(b"")
        found = paths.conversation_databases(make_env(tmp_path))
        assert [path.name for path in found] == ["a.db", "b.db"]

    def test_conversation_id_comes_from_the_filename(self) -> None:
        assert paths.conversation_id_of(Path(f"{CONV}.db")) == UUID(CONV)
        assert paths.conversation_id_of(Path("notes.db")) is None


class TestAttachments:
    def test_both_filename_spellings_are_found(self, tmp_path: Path) -> None:
        """Real uploads use ``media_`` *and* ``media__``.

        The plan records only the first. Globbing on it would have dropped
        every attachment in the conversations that use the other.
        """
        directory = tmp_path / "antigravity" / "brain" / CONV / ".user_uploaded"
        directory.mkdir(parents=True)
        (directory / "media_1786163647832.png").write_bytes(b"a")
        (directory / "media__1786017081245.png").write_bytes(b"b")
        found = paths.attachment_files(CONV, make_env(tmp_path))
        assert len(found) == 2

    def test_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        assert paths.attachment_files(CONV, make_env(tmp_path)) == []


class TestProjects:
    def _write(self, tmp_path: Path, name: str, document: dict) -> None:
        directory = tmp_path / "config" / "projects"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(json.dumps(document), encoding="utf-8")

    def test_reads_a_plain_folder_record(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "p1.json",
            {
                "id": "p1",
                "name": "Project One",
                "projectResources": {"resources": [{"folderUri": "file:///c%3A%5Cwork"}]},
            },
        )
        found = paths.projects(make_env(tmp_path))
        assert found["p1"].folder == "file:///c%3A%5Cwork"

    def test_reads_a_git_folder_record(self, tmp_path: Path) -> None:
        """A folder under git is nested one level deeper.

        Reading only the plain shape reports no folder at all -- for exactly
        the projects where the folder matters most.
        """
        self._write(
            tmp_path,
            "p2.json",
            {
                "id": "p2",
                "name": "Two",
                "projectResources": {
                    "resources": [{"gitFolder": {"folderUri": "file:///c%3A%5Cgit"}}]
                },
            },
        )
        assert paths.projects(make_env(tmp_path))["p2"].folder == "file:///c%3A%5Cgit"

    def test_a_record_with_no_folder_still_loads(self, tmp_path: Path) -> None:
        self._write(tmp_path, "p3.json", {"id": "outside", "name": "Outside of Project"})
        found = paths.projects(make_env(tmp_path))
        assert found["outside"].folder is None

    def test_unreadable_json_is_skipped_not_raised(self, tmp_path: Path) -> None:
        directory = tmp_path / "config" / "projects"
        directory.mkdir(parents=True)
        (directory / "broken.json").write_text("{ not json", encoding="utf-8")
        self._write(tmp_path, "ok.json", {"id": "ok", "name": "Fine"})
        assert list(paths.projects(make_env(tmp_path))) == ["ok"]


class TestOpenReadonly:
    def test_reads_a_database(self, tmp_path: Path) -> None:
        database = build_database(
            tmp_path / "antigravity" / "conversations" / f"{CONV}.db", [(0, 14, "hello")]
        )
        with paths.open_readonly(database) as connection:
            assert connection.execute("SELECT count(*) FROM steps").fetchone()[0] == 1

    def test_refuses_to_write(self, tmp_path: Path) -> None:
        """The store Ferry promised only to read must be impossible to write."""
        database = build_database(
            tmp_path / "antigravity" / "conversations" / f"{CONV}.db", [(0, 14, "hello")]
        )
        with paths.open_readonly(database) as connection:
            try:
                connection.execute("DELETE FROM steps")
            except sqlite3.OperationalError:
                return
            raise AssertionError("a read-only connection accepted a write")

    def test_falls_back_to_a_copy_when_the_wal_cannot_be_opened(self, tmp_path: Path) -> None:
        """A WAL database with no ``-shm`` cannot be opened read-only.

        SQLite would have to create the ``-shm`` file to read it, and Ferry
        must not create anything in the user's store -- so it copies instead.
        """
        database = build_database(
            tmp_path / "antigravity" / "conversations" / f"{CONV}.db", [(0, 14, "hi")]
        )
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO steps (idx, step_type) VALUES (99, 14)")
        connection.commit()
        connection.close()

        with paths.open_readonly(database) as opened:
            assert opened.execute("SELECT count(*) FROM steps").fetchone()[0] == 2


class TestVersion:
    def test_unknown_when_nothing_is_installed(self, tmp_path: Path) -> None:
        env = {paths.INSTALL_DIR_ENV: str(tmp_path / "nowhere")}
        assert paths.antigravity_version(env) is None

    def test_unknown_rather_than_wrong_when_the_archive_is_unreadable(self, tmp_path: Path) -> None:
        resources = tmp_path / "install" / "resources"
        resources.mkdir(parents=True)
        (resources / "app.asar").write_bytes(b"not an archive")
        env = {paths.INSTALL_DIR_ENV: str(tmp_path / "install")}
        assert paths.antigravity_version(env) is None
