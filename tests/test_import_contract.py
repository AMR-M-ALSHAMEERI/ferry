"""The promises every adapter's import makes, asked of all four.

``ImportOptions`` has carried ``backup``, ``dry_run`` and ``on_conflict`` since
M3, and each adapter was tested against them in its own file -- except Copilot,
whose import ignored the options object entirely for two milestones. Nothing
caught it because nothing ever asked Copilot the questions the other three were
being asked, and the CLI passed the defaults, so the gap was invisible right up
until M7 made those options reachable.

**A dry run into Copilot Chat would have written**: a row into VS Code's chat
index and a transcript file. That is the failure this file exists to make
impossible to reintroduce -- for the fifth adapter as much as for these four.

Each test states the promise once and runs it against every tool, so a new
adapter inherits the whole contract by being added to ``BUILDERS``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ferry.adapters.base import ImportEvent, ImportOptions
from ferry.core.backup import backup_root
from tests.importcontract import BUILDERS, RENAME_IMPOSSIBLE, Case

TOOLS = sorted(BUILDERS)


@pytest.fixture(params=TOOLS)
def case(request: pytest.FixtureRequest, tmp_path: Path) -> Case:
    return BUILDERS[request.param](tmp_path)


def run(case: Case, options: ImportOptions) -> list[ImportEvent]:
    return list(case.adapter.import_(case.bundle, options))


def test_every_adapter_can_import_what_it_exported(case: Case) -> None:
    """The floor. Without this the rest of the file proves nothing."""
    events = run(case, ImportOptions())

    assert not [event for event in events if event.kind == "error"], [
        event.message for event in events if event.kind == "error"
    ]
    assert any(event.kind == "progress" for event in events)
    assert case.written(), f"{case.name} reported success and wrote nothing"


def test_a_dry_run_writes_nothing(case: Case) -> None:
    """The promise with the sharpest edge.

    Someone asks what an import *would* do precisely because they are not sure
    they want it. An adapter that writes anyway has done the one thing they
    were trying to avoid, and told them it was only a preview.
    """
    events = run(case, ImportOptions(dry_run=True))

    assert case.written() == [], f"{case.name} wrote during a dry run"
    assert any(event.kind == "progress" for event in events), (
        "a dry run must still say what it would have done"
    )


def test_a_dry_run_says_what_it_would_do_without_committing_to_having_done_it(
    case: Case,
) -> None:
    """Wording, and it matters: past tense in a preview reads as a report."""
    events = run(case, ImportOptions(dry_run=True))
    progress = [event.message for event in events if event.kind == "progress"]

    assert progress
    assert all("would" in message for message in progress), progress


def test_skip_is_the_default_and_leaves_what_is_there_alone(case: Case) -> None:
    run(case, ImportOptions())
    before = {path: path.read_bytes() for path in case.written()}

    events = run(case, ImportOptions())

    assert any(event.kind == "skipped" for event in events)
    assert {path: path.read_bytes() for path in case.written()} == before


def test_overwrite_copies_the_old_file_into_the_backup_directory_first(
    case: Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backups go where Ferry says backups go.

    Three adapters wrote to ``~/.ferry/backups`` and Antigravity left a ``.bak``
    beside the original -- inside the store the tool itself reads. A backup
    nobody can find is not one.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    run(case, ImportOptions())
    assert case.written()

    run(case, ImportOptions(on_conflict="overwrite"))

    backups = [path for path in backup_root().rglob("*") if path.is_file()]
    assert backups, f"{case.name} overwrote without taking a backup"
    assert not any(path.suffix == ".bak" for path in case.store.rglob("*")), (
        f"{case.name} left a backup inside the tool's own store"
    )


def test_a_backup_records_where_the_file_came_from(
    case: Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy with no record of its original cannot be put back."""
    from ferry.core import read_manifest

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    run(case, ImportOptions())
    run(case, ImportOptions(on_conflict="overwrite"))

    runs = [path for path in backup_root().iterdir() if path.is_dir()]
    assert len(runs) == 1, "one import is one backup directory"
    records = read_manifest(runs[0])
    assert records
    for record in records:
        assert record.original.exists() or record.original.name
        assert record.stored.is_file()


def test_backup_can_be_turned_off(
    case: Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    run(case, ImportOptions())
    run(case, ImportOptions(on_conflict="overwrite", backup=False))

    assert not backup_root().exists()


def test_rename_keeps_both_or_refuses_but_never_silently_overwrites(case: Case) -> None:
    """Keeping both means a second conversation, not a second filename.

    All four tools put the conversation id in the filename *and* inside the
    file. A copy under a different name still claiming the original id is a
    conversation that contradicts itself, so rename imports a new identity --
    or, where the id is written through storage Ferry cannot re-identify, says
    so and writes nothing.
    """
    run(case, ImportOptions())
    first = {path: path.read_bytes() for path in case.written()}
    assert first

    events = run(case, ImportOptions(on_conflict="rename"))
    after = case.written()

    if case.name in RENAME_IMPOSSIBLE:
        assert any(event.kind == "skipped" for event in events)
        assert {path: path.read_bytes() for path in after} == first
        return

    assert len(after) == len(first) + 1, f"{case.name} did not keep both copies"
    for path, content in first.items():
        assert path.read_bytes() == content, "the conversation already there was modified"


def test_a_refusal_explains_itself(case: Case) -> None:
    """Every skip carries a reason. "skipped" alone is not an answer."""
    run(case, ImportOptions())
    events = run(case, ImportOptions())

    for event in events:
        if event.kind == "skipped":
            assert event.message.strip(), f"{case.name} skipped without saying why"


def test_a_backup_is_announced_once_and_as_a_note(
    case: Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Taking a backup is the safe path working, not a thing to be warned about.

    Importing four Copilot conversations printed **six warning lines**, one per
    file copied, each carrying the same marker as "this conversation lost its
    thinking blocks" and each repeating an absolute path long enough to wrap the
    terminal. A marker that appears on everything stops meaning anything, which
    is the fault of ledger #159 arriving by a different route.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    run(case, ImportOptions())
    events = run(case, ImportOptions(on_conflict="overwrite"))

    about_backups = [
        event for event in events if "copied to" in event.message or "saved to" in event.message
    ]
    assert not [event for event in about_backups if event.kind == "warning"], (
        f"{case.name} reported a routine backup as a warning"
    )
    assert len(about_backups) <= 1, f"{case.name} said the same thing {len(about_backups)} times"
