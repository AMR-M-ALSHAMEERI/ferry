"""Copies of what an import overwrote, and the record of where they came from.

The behaviour under test is small, and all of it was wrong in at least one
adapter before this module existed: a per-file timestamp that split one import
across directories, a second backup that replaced the first, and a copy with no
note of its original path -- which makes it useless for the only thing a backup
is for.
"""

from __future__ import annotations

import json
from pathlib import Path

from ferry.core.backup import MANIFEST_NAME, back_up, backup_root, read_manifest


def _source(tmp_path: Path, name: str = "chat.jsonl", text: str = "first") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_the_copy_holds_what_the_original_held(tmp_path: Path) -> None:
    source = _source(tmp_path)

    stored = back_up(source, "codex", root=tmp_path / "backups")

    assert stored.read_text(encoding="utf-8") == "first"
    assert stored.parent.name == "codex"


def test_two_tools_with_the_same_filename_do_not_collide(tmp_path: Path) -> None:
    """Claude Code, Codex and Copilot all name a transcript ``<uuid>.jsonl``."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    one = _source(tmp_path / "a", text="claude")
    two = _source(tmp_path / "b", text="codex")

    first = back_up(one, "claude_code", root=tmp_path / "backups")
    second = back_up(two, "codex", root=tmp_path / "backups")

    assert first != second
    assert first.read_text(encoding="utf-8") == "claude"
    assert second.read_text(encoding="utf-8") == "codex"


def test_backing_the_same_file_up_twice_keeps_the_first_copy(tmp_path: Path) -> None:
    """The first copy is the state the user actually started from.

    Reached when one file is overwritten twice inside a single run. Replacing
    the earlier copy would lose exactly the version worth keeping.
    """
    source = _source(tmp_path, text="original")
    first = back_up(source, "codex", root=tmp_path / "backups")

    source.write_text("second", encoding="utf-8")
    second = back_up(source, "codex", root=tmp_path / "backups")

    assert first != second
    assert first.read_text(encoding="utf-8") == "original"
    assert second.read_text(encoding="utf-8") == "second"


def test_one_run_is_one_directory(tmp_path: Path) -> None:
    """The stamp is taken once per Ferry run, not once per file.

    Computed per file, an import that crossed a second boundary split into two
    directories and read as two events.
    """
    root = tmp_path / "backups"
    for index in range(3):
        path = tmp_path / f"chat{index}.jsonl"
        path.write_text(str(index), encoding="utf-8")
        back_up(path, "codex", root=root)

    runs = [child for child in root.iterdir() if child.is_dir()]
    assert len(runs) == 1


def test_the_manifest_names_where_each_copy_came_from(tmp_path: Path) -> None:
    """A copy with no record of its original cannot be put back."""
    root = tmp_path / "backups"
    source = _source(tmp_path)
    stored = back_up(source, "codex", root=root)

    run = next(child for child in root.iterdir() if child.is_dir())
    records = read_manifest(run)

    assert len(records) == 1
    assert records[0].original == source
    assert records[0].stored == stored
    assert records[0].tool == "codex"
    assert records[0].backed_up_at.tzinfo is not None


def test_a_manifest_line_that_will_not_parse_does_not_hide_the_others(tmp_path: Path) -> None:
    """This is what someone reads when they want a file back."""
    root = tmp_path / "backups"
    back_up(_source(tmp_path), "codex", root=root)
    run = next(child for child in root.iterdir() if child.is_dir())

    manifest = run / MANIFEST_NAME
    good = manifest.read_text(encoding="utf-8")
    manifest.write_text("{ not json\n" + good + json.dumps({"tool": "codex"}) + "\n", "utf-8")

    records = read_manifest(run)

    assert len(records) == 1, "the intact line must survive a broken one on either side"


def test_a_directory_with_no_manifest_reads_as_empty_rather_than_raising(tmp_path: Path) -> None:
    assert read_manifest(tmp_path / "never-written") == []


def test_the_production_root_is_under_the_users_home(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert backup_root() == tmp_path / ".ferry" / "backups"
