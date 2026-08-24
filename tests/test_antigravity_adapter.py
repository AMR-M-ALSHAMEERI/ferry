"""Detect, export, and the path-remapped import M6 exits on."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import UUID

import pytest

from ferry.adapters.antigravity import paths, schema, wire
from ferry.adapters.antigravity.adapter import PROJECT_SIDECAR, AntigravityAdapter
from ferry.adapters.base import ImportOptions
from ferry.core import Bundle
from tests.antigravity_fixture import build_database

CONV = UUID("aaaaaaaa-1111-4111-8111-111111111111")
OTHER = UUID("bbbbbbbb-2222-4222-8222-222222222222")
PROJECT = "99999999-9999-4999-8999-999999999999"

STEPS = [
    (0, schema.USER_INPUT, r"look at C:\Users\Dell\Work\main.py"),
    (1, schema.PLANNER_RESPONSE, r"I read C:/Users/Dell/Work/main.py"),
    (2, 21, r"cd file:///c%3A%5CUsers%5CDell%5CWork"),
]


@pytest.fixture
def source(tmp_path: Path) -> dict[str, str]:
    """A populated Antigravity store, with a project record."""
    root = tmp_path / "src"
    build_database(root / "antigravity" / "conversations" / f"{CONV}.db", STEPS)
    projects = root / "config" / "projects"
    projects.mkdir(parents=True)
    (projects / f"{PROJECT}.json").write_text(
        json.dumps(
            {
                "id": PROJECT,
                "name": "Work",
                "projectResources": {
                    "resources": [{"folderUri": "file:///c%3A%5CUsers%5CDell%5CWork"}]
                },
            }
        ),
        encoding="utf-8",
    )
    return {paths.DATA_DIR_ENV: str(root / "antigravity")}


@pytest.fixture
def target(tmp_path: Path) -> dict[str, str]:
    """An empty Antigravity store to import into."""
    return {paths.DATA_DIR_ENV: str(tmp_path / "dest" / "antigravity")}


def run(events) -> dict[str, list[str]]:
    collected: dict[str, list[str]] = {}
    for event in events:
        collected.setdefault(event.kind, []).append(event.message)
    return collected


class TestDetect:
    def test_reports_not_installed_when_there_is_no_data(self, tmp_path: Path) -> None:
        found = AntigravityAdapter({paths.DATA_DIR_ENV: str(tmp_path / "none")}).detect()
        assert not found.installed
        assert found.notes

    def test_an_empty_store_is_not_installed_but_says_where_it_looked(self, tmp_path: Path) -> None:
        root = tmp_path / "antigravity"
        root.mkdir(parents=True)
        found = AntigravityAdapter({paths.DATA_DIR_ENV: str(root)}).detect()
        assert not found.installed
        assert found.data_paths == [root]

    def test_counts_conversations(self, source: dict[str, str]) -> None:
        found = AntigravityAdapter(source).detect()
        assert found.installed
        assert found.conversation_count_estimate == 1


class TestFormatCaveat:
    """The screen stays quiet through an update that changed nothing.

    Antigravity ships often. A caveat tied to the version number would appear
    days after this was built and never leave, and by the time the format
    really changed nobody would be reading it. What is checked is the format
    -- see tests/test_formatcheck.py for the checks themselves.
    """

    def test_a_healthy_store_says_nothing_whatever_the_version(
        self, source: dict[str, str], monkeypatch
    ) -> None:
        monkeypatch.setattr(paths, "antigravity_version", lambda env=None: "99.0.0")
        assert AntigravityAdapter(source).detect().caveats == []


class TestExport:
    def test_writes_the_conversation_and_the_original_database(
        self, source: dict[str, str], tmp_path: Path
    ) -> None:
        bundle_dir = tmp_path / "bundle"
        events = run(AntigravityAdapter(source).export(bundle_dir))
        assert len(events["progress"]) == 1

        bundle = Bundle.open(bundle_dir)
        assert bundle.list_conversations() == [CONV]
        assert bundle.has_source_raw(CONV)

    def test_the_carried_database_is_readable(self, source: dict[str, str], tmp_path: Path) -> None:
        """It is what an import restores from, so it has to be a real database."""
        bundle_dir = tmp_path / "bundle"
        run(AntigravityAdapter(source).export(bundle_dir))
        copy = Bundle.open(bundle_dir).source_raw_path(CONV)
        connection = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            assert connection.execute("SELECT count(*) FROM steps").fetchone()[0] == len(STEPS)
        finally:
            connection.close()

    def test_the_project_record_travels_with_it(
        self, source: dict[str, str], tmp_path: Path
    ) -> None:
        bundle_dir = tmp_path / "bundle"
        run(AntigravityAdapter(source).export(bundle_dir))
        sidecar = Bundle.open(bundle_dir).source_raw_sidecar_dir(CONV) / PROJECT_SIDECAR
        assert sidecar.is_file()

    def test_re_export_skips_what_is_already_there(
        self, source: dict[str, str], tmp_path: Path
    ) -> None:
        """An interrupted export must be resumable."""
        bundle_dir = tmp_path / "bundle"
        adapter = AntigravityAdapter(source)
        run(adapter.export(bundle_dir))
        again = run(adapter.export(bundle_dir))
        assert again.get("progress") is None
        assert "already in bundle" in again["skipped"][0]

    def test_an_empty_store_reports_an_error_not_a_crash(self, tmp_path: Path) -> None:
        events = run(
            AntigravityAdapter({paths.DATA_DIR_ENV: str(tmp_path / "none")}).export(
                tmp_path / "bundle"
            )
        )
        assert events["error"]


def blob_strings(database: Path) -> list[str]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    found: list[str] = []
    try:
        for (value,) in connection.execute("SELECT step_payload FROM steps"):
            if isinstance(value, bytes | bytearray) and value:
                found.extend(text for _p, text in wire.strings(bytes(value)))
    finally:
        connection.close()
    return found


class TestImport:
    def _bundle(self, source: dict[str, str], tmp_path: Path) -> Path:
        bundle_dir = tmp_path / "bundle"
        run(AntigravityAdapter(source).export(bundle_dir))
        return bundle_dir

    def test_writes_the_conversation_into_an_empty_store(
        self, source: dict[str, str], target: dict[str, str], tmp_path: Path
    ) -> None:
        bundle_dir = self._bundle(source, tmp_path)
        events = run(AntigravityAdapter(target).import_(bundle_dir, ImportOptions()))
        assert len(events["progress"]) == 1
        assert (paths.conversations_dir(target) / f"{CONV}.db").is_file()

    def test_an_import_with_no_remap_changes_nothing_in_the_bytes(
        self, source: dict[str, str], target: dict[str, str], tmp_path: Path
    ) -> None:
        """The guarantee the whole codec is built to provide."""
        bundle_dir = self._bundle(source, tmp_path)
        run(AntigravityAdapter(target).import_(bundle_dir, ImportOptions()))
        original = Bundle.open(bundle_dir).source_raw_path(CONV).read_bytes()
        written = (paths.conversations_dir(target) / f"{CONV}.db").read_bytes()
        assert written == original

    @pytest.mark.parametrize(
        "spelling",
        [r"C:\Users\Dell", "C:/Users/Dell", "file:///c%3A%5CUsers%5CDell"],
    )
    def test_every_spelling_of_the_old_path_is_gone(
        self,
        source: dict[str, str],
        target: dict[str, str],
        tmp_path: Path,
        spelling: str,
    ) -> None:
        bundle_dir = self._bundle(source, tmp_path)
        options = ImportOptions(path_remap=((r"C:\Users\Dell", r"D:\home\amr-longer"),))
        run(AntigravityAdapter(target).import_(bundle_dir, options))
        written = blob_strings(paths.conversations_dir(target) / f"{CONV}.db")
        assert not any(spelling.lower() in text.lower() for text in written)

    def test_the_remapped_conversation_still_reads(
        self, source: dict[str, str], target: dict[str, str], tmp_path: Path
    ) -> None:
        """Corrupting a length prefix produces bytes that usually still parse.

        So "it opened" proves nothing on its own -- the messages have to come
        back, in the same number, with the new path in them.
        """
        from ferry.adapters.antigravity import reader

        bundle_dir = self._bundle(source, tmp_path)
        options = ImportOptions(path_remap=((r"C:\Users\Dell", r"D:\home\amr-longer"),))
        run(AntigravityAdapter(target).import_(bundle_dir, options))

        found = reader.read_conversation(paths.conversations_dir(target) / f"{CONV}.db", target)
        assert found.conversation is not None
        assert len(found.conversation.messages) == len(STEPS)
        assert r"D:\home\amr-longer\Work\main.py" in found.conversation.messages[0].content[0].text

    def test_the_project_record_is_recreated_and_remapped(
        self, source: dict[str, str], target: dict[str, str], tmp_path: Path
    ) -> None:
        bundle_dir = self._bundle(source, tmp_path)
        options = ImportOptions(path_remap=((r"C:\Users\Dell", r"D:\home\amr"),))
        run(AntigravityAdapter(target).import_(bundle_dir, options))
        written = json.loads(
            (paths.projects_dir(target) / f"{PROJECT}.json").read_text(encoding="utf-8")
        )
        uri = written["projectResources"]["resources"][0]["folderUri"]
        assert uri == "file:///d%3A%5Chome%5Camr%5CWork"

    def test_an_existing_project_record_is_left_alone(
        self, source: dict[str, str], target: dict[str, str], tmp_path: Path
    ) -> None:
        """It describes a folder on *this* machine; the bundle's does not.

        Overwriting it would re-point every conversation already filed there.
        """
        bundle_dir = self._bundle(source, tmp_path)
        existing = paths.projects_dir(target)
        existing.mkdir(parents=True)
        (existing / f"{PROJECT}.json").write_text('{"id": "kept"}', encoding="utf-8")
        run(AntigravityAdapter(target).import_(bundle_dir, ImportOptions()))
        assert (existing / f"{PROJECT}.json").read_text(encoding="utf-8") == '{"id": "kept"}'

    def test_a_conversation_from_another_tool_is_skipped(
        self, source: dict[str, str], target: dict[str, str], tmp_path: Path, conversation
    ) -> None:
        bundle_dir = self._bundle(source, tmp_path)
        Bundle.open(bundle_dir).add_conversation(conversation)
        events = run(AntigravityAdapter(target).import_(bundle_dir, ImportOptions()))
        assert any("claude-code" in message for message in events["skipped"])

    def test_a_conversation_with_no_database_is_skipped_not_half_written(
        self, source: dict[str, str], target: dict[str, str], tmp_path: Path, conversation
    ) -> None:
        bundle_dir = self._bundle(source, tmp_path)
        foreign = conversation.model_copy(update={"id": OTHER, "source_tool": "antigravity"})
        Bundle.open(bundle_dir).add_conversation(foreign)
        options = ImportOptions(allow_cross_tool=True)
        events = run(AntigravityAdapter(target).import_(bundle_dir, options))
        assert any("no original database" in message for message in events["skipped"])
        assert not (paths.conversations_dir(target) / f"{OTHER}.db").exists()

    def test_an_existing_conversation_is_skipped_by_default(
        self, source: dict[str, str], target: dict[str, str], tmp_path: Path
    ) -> None:
        bundle_dir = self._bundle(source, tmp_path)
        adapter = AntigravityAdapter(target)
        run(adapter.import_(bundle_dir, ImportOptions()))
        again = run(adapter.import_(bundle_dir, ImportOptions()))
        assert "already in Antigravity" in again["skipped"][0]

    def test_overwrite_makes_a_backup_first(
        self, source: dict[str, str], target: dict[str, str], tmp_path: Path
    ) -> None:
        bundle_dir = self._bundle(source, tmp_path)
        adapter = AntigravityAdapter(target)
        run(adapter.import_(bundle_dir, ImportOptions()))
        run(adapter.import_(bundle_dir, ImportOptions(on_conflict="overwrite")))
        backups = list(paths.conversations_dir(target).glob(f"{CONV}.db.*.bak"))
        assert backups

    def test_dry_run_writes_nothing(
        self, source: dict[str, str], target: dict[str, str], tmp_path: Path
    ) -> None:
        bundle_dir = self._bundle(source, tmp_path)
        events = run(AntigravityAdapter(target).import_(bundle_dir, ImportOptions(dry_run=True)))
        assert "would write" in events["progress"][0]
        assert not (paths.conversations_dir(target) / f"{CONV}.db").exists()

    def test_an_empty_bundle_reports_an_error(self, tmp_path: Path, target: dict[str, str]) -> None:
        events = run(AntigravityAdapter(target).import_(tmp_path / "nothing", ImportOptions()))
        assert events["error"]

    def test_stale_wal_sidecars_are_removed(
        self, source: dict[str, str], target: dict[str, str], tmp_path: Path
    ) -> None:
        """They would describe the conversation that used to be at that name."""
        bundle_dir = self._bundle(source, tmp_path)
        destination = paths.conversations_dir(target)
        destination.mkdir(parents=True)
        (destination / f"{CONV}.db").write_bytes(b"old")
        (destination / f"{CONV}.db-wal").write_bytes(b"stale")
        run(AntigravityAdapter(target).import_(bundle_dir, ImportOptions(on_conflict="overwrite")))
        assert not (destination / f"{CONV}.db-wal").exists()


class TestSubagentCount:
    """What Ferry reports must match what Antigravity lists.

    A subagent trajectory has its own database and looks exactly like a
    conversation, but the app never shows it. On the reference machine that is
    **6 databases for the 2 conversations the user sees** - so counting files
    reported three times as many conversations as exist.

    Nothing is dropped: the subagent databases are still exported, because the
    conversation that spawned them refers to them and an import without them
    would restore a conversation with pieces missing.
    """

    def _store(self, tmp_path: Path) -> dict[str, str]:
        root = tmp_path / "tree"
        conversations = root / "antigravity" / "conversations"
        build_database(conversations / f"{CONV}.db", STEPS)
        for index in range(3):
            child = UUID(f"cccccccc-{index}333-4333-8333-333333333333")
            build_database(conversations / f"{child}.db", STEPS, parent=str(CONV))
        return {paths.DATA_DIR_ENV: str(root / "antigravity")}

    def test_detect_counts_conversations_not_files(self, tmp_path: Path) -> None:
        found = AntigravityAdapter(self._store(tmp_path)).detect()
        assert found.conversation_count_estimate == 1
        assert any("3 subagent trajectories" in note for note in found.notes)

    def test_export_still_carries_every_database(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        bundle_dir = tmp_path / "bundle"
        events = run(AntigravityAdapter(store).export(bundle_dir))
        assert len(events["progress"]) == 4
        assert len(Bundle.open(bundle_dir).list_conversations()) == 4

    def test_the_done_line_gives_both_numbers(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        events = run(AntigravityAdapter(store).export(tmp_path / "bundle"))
        assert events["done"][0] == (
            "1 of 1 conversations exported, plus 3 subagent trajectories they spawned"
        )

    def test_a_subagent_is_labelled_in_its_progress_line(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        events = run(AntigravityAdapter(store).export(tmp_path / "bundle"))
        assert sum(1 for m in events["progress"] if m.startswith("subagent of")) == 3
