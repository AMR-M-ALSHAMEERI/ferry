"""Clearing out backups: only what Ferry made, only what the person chose.

Backups are the one thing Ferry keeps so that a mistake can be undone, which
makes deleting them the one action with no undo of its own. Three promises carry
the weight: nothing that is not a backup folder Ferry made is ever touched; a
backup is called a leftover only when every copy in it came from a temporary
folder; and nothing goes without a yes.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ferry.cli.flows import run_cleanup
from ferry.core.backup import MANIFEST_NAME, backup_root
from ferry.core.cleanup import (
    BUNDLE,
    SETTINGS,
    TEMPORARY,
    BackupRun,
    delete_runs,
    list_runs,
    newest_by_tool,
)
from tests.test_flows import _Answers

TEMP = Path(tempfile.gettempdir())
REAL = "C:/Users/someone/.codex/sessions/2026/09/01/rollout.jsonl"
"""Somewhere that is neither a temporary folder nor a pytest folder, on any OS."""


def make_run(name: str, *copies: tuple[str, str], size: int = 100) -> Path:
    """A backup folder as ``back_up`` lays one out: tool folders and a manifest."""
    run = backup_root() / name
    lines = []
    for index, (tool, original) in enumerate(copies):
        stored = f"copy-{index}{Path(original).suffix}"
        (run / tool).mkdir(parents=True, exist_ok=True)
        (run / tool / stored).write_bytes(b"x" * size)
        lines.append(
            json.dumps(
                {
                    "backed_up_at": "2026-09-01T00:00:00Z",
                    "tool": tool,
                    "original": original,
                    "stored": stored,
                }
            )
        )
    run.mkdir(parents=True, exist_ok=True)
    (run / MANIFEST_NAME).write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return run


def temporary(name: str = "a.db") -> str:
    return str(TEMP / "somewhere" / name)


class TestListing:
    def test_only_folders_ferry_named_are_listed(self) -> None:
        make_run("20260901T000000Z", ("codex", REAL))
        (backup_root() / "my-notes").mkdir()
        (backup_root() / "20260902T000000Z.txt").write_text("not a folder", encoding="utf-8")

        assert [run.path.name for run in list_runs()] == ["20260901T000000Z"]

    def test_newest_first(self) -> None:
        make_run("20260901T000000Z", ("codex", REAL))
        make_run("20260903T000000Z", ("codex", REAL))
        make_run("20260902T000000Z", ("codex", REAL))

        assert [run.path.name for run in list_runs()] == [
            "20260903T000000Z",
            "20260902T000000Z",
            "20260901T000000Z",
        ]

    def test_size_and_copies_are_counted(self) -> None:
        make_run("20260901T000000Z", ("codex", REAL), ("codex", REAL), size=1000)

        [run] = list_runs()

        assert run.copies == 2
        assert run.size >= 2000

    def test_a_backup_of_temporary_copies_is_a_leftover(self) -> None:
        make_run("20260901T000000Z", ("copilot", temporary()), ("antigravity", temporary("b.db")))

        [run] = list_runs()

        assert run.holds == (TEMPORARY,)
        assert run.from_temporary

    def test_a_pytest_folder_counts_as_temporary_wherever_it_is(self) -> None:
        make_run("20260901T000000Z", ("codex", "C:/work/.pytest-tmp/test_x0/a.jsonl"))

        [run] = list_runs()

        assert run.from_temporary

    def test_one_real_copy_makes_it_the_persons_to_decide(self) -> None:
        make_run("20260901T000000Z", ("copilot", temporary()), ("codex", REAL))

        [run] = list_runs()

        assert not run.from_temporary
        assert run.holds == ("codex", TEMPORARY)
        assert run.tools == ("codex",)

    def test_bundle_and_settings_copies_are_named_for_what_they_are(self) -> None:
        make_run(
            "20260901T000000Z",
            ("bundle-my-backup", "C:/Users/someone/Desktop/my-backup/conversations/a.json"),
            ("claude-code", "C:/Users/someone/.claude.json"),
        )

        [run] = list_runs()

        assert set(run.holds) == {BUNDLE, SETTINGS}
        assert run.tools == ()

    def test_a_backup_with_no_manifest_is_never_called_a_leftover(self) -> None:
        run = backup_root() / "20260901T000000Z"
        (run / "codex").mkdir(parents=True)
        (run / "codex" / "a.jsonl").write_text("{}", encoding="utf-8")

        [found] = list_runs()

        assert not found.from_temporary

    def test_the_newest_backup_for_each_assistant(self) -> None:
        make_run("20260901T000000Z", ("codex", REAL), ("copilot", REAL.replace(".codex", "Code")))
        make_run("20260902T000000Z", ("codex", REAL))

        newest = newest_by_tool(list_runs())

        assert newest["codex"].path.name == "20260902T000000Z"
        assert newest["copilot"].path.name == "20260901T000000Z"

    def test_no_backups_folder_is_an_empty_list(self, tmp_path: Path) -> None:
        assert list_runs(tmp_path / "absent") == []


class TestDeleting:
    def test_only_the_chosen_backups_go(self) -> None:
        make_run("20260901T000000Z", ("codex", REAL), size=500)
        make_run("20260902T000000Z", ("codex", REAL))
        doomed = [run for run in list_runs() if run.path.name == "20260901T000000Z"]

        cleared = delete_runs(doomed)

        assert cleared.deleted == 1
        assert cleared.freed == doomed[0].size
        assert [run.path.name for run in list_runs()] == ["20260902T000000Z"]

    def test_a_folder_that_is_not_a_backup_is_refused(self, tmp_path: Path) -> None:
        elsewhere = tmp_path / "20260901T000000Z"
        elsewhere.mkdir()
        (elsewhere / "precious.txt").write_text("mine", encoding="utf-8")
        impostor = BackupRun(elsewhere, datetime.now(UTC), 4, 1, ("codex",))

        cleared = delete_runs([impostor])

        assert cleared.deleted == 0
        assert cleared.problems
        assert (elsewhere / "precious.txt").is_file()

    def test_a_folder_inside_the_backups_not_named_by_ferry_is_refused(self) -> None:
        mine = backup_root() / "keep-this"
        mine.mkdir(parents=True)
        run = BackupRun(mine, datetime.now(UTC), 0, 0, ("codex",))

        cleared = delete_runs([run])

        assert mine.is_dir()
        assert cleared.problems

    def test_read_only_copies_are_deleted_too(self) -> None:
        """``copy2`` carries a read-only flag over from the original."""
        path = make_run("20260901T000000Z", ("codex", REAL))
        for item in path.rglob("*"):
            if item.is_file():
                os.chmod(item, stat.S_IREAD)

        cleared = delete_runs(list_runs())

        assert cleared.deleted == 1
        assert not path.exists()


class TestTheScreen:
    def test_with_no_backups_it_says_so_and_asks_nothing(self) -> None:
        ui = _Answers()

        run_cleanup(ui)

        assert "There are no backups" in ui.text
        assert ui.asked == 0

    def test_the_leftovers_can_go_in_one_step_and_nothing_else_does(self) -> None:
        make_run("20260901T000000Z", ("copilot", temporary()))
        make_run("20260902T000000Z", ("codex", REAL))
        ui = _Answers(actions=["temporary"], confirm=True)

        run_cleanup(ui)

        assert [run.path.name for run in list_runs()] == ["20260902T000000Z"]
        assert "Deleted 1 backup" in ui.text

    def test_the_leftover_offer_is_absent_when_there_are_none(self) -> None:
        make_run("20260902T000000Z", ("codex", REAL))
        ui = _Answers(actions=["cancel"])

        run_cleanup(ui)

        assert "temporary folders" not in ui.text
        assert len(list_runs()) == 1

    def test_saying_no_deletes_nothing(self) -> None:
        make_run("20260901T000000Z", ("copilot", temporary()))
        ui = _Answers(actions=["temporary"], confirm=False)

        run_cleanup(ui)

        assert len(list_runs()) == 1
        assert "Nothing was deleted." in ui.text

    def test_choosing_starts_with_nothing_ticked_and_deletes_what_was_ticked(self) -> None:
        make_run("20260901T000000Z", ("codex", REAL))
        make_run("20260902T000000Z", ("codex", REAL))
        ui = _Answers(actions=["choose"], ticks=[["20260901T000000Z"]], confirm=True)

        run_cleanup(ui)

        assert ui.preselected == [[]]
        assert [run.path.name for run in list_runs()] == ["20260902T000000Z"]

    def test_ticking_nothing_asks_nothing_more(self) -> None:
        make_run("20260901T000000Z", ("codex", REAL))
        ui = _Answers(actions=["choose"], ticks=[[]])

        run_cleanup(ui)

        assert ui.asked_to_confirm == 0
        assert len(list_runs()) == 1

    def test_the_newest_backup_for_an_assistant_is_named_before_it_goes(self) -> None:
        make_run("20260901T000000Z", ("codex", REAL))
        ui = _Answers(actions=["choose"], ticks=[["20260901T000000Z"]], confirm=False)

        run_cleanup(ui)

        assert "newest backup for codex" in ui.text
        assert len(list_runs()) == 1


@pytest.fixture(autouse=True)
def _backups_start_empty() -> None:
    """Each test gets its own backups folder from conftest; make sure it is empty."""
    assert not backup_root().exists() or not any(backup_root().iterdir())
