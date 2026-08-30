r"""Ferry noticing that Claude Code will refuse to open what Ferry just wrote.

Found by a person, not by this suite: an imported conversation appeared in
``claude --resume`` and then refused to open with *"That session's folder isn't
trusted yet."* Every test in the adapter suite was green, because they all
assert about the file Ferry writes and the refusal lives in a file Ferry had
never heard of.

The fixture below is the shape of the real ``~/.claude.json`` that produced it,
including the detail that made the exact-match rule necessary: one folder,
spelled two ways, with two different answers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ferry.adapters.claude_code.trust import (
    TrustError,
    advice,
    config_file,
    grant,
    is_trusted,
)

TRUSTED = r"C:\Users\Dell\Desktop\Ferry"
SLASHED = "C:/Users/Dell/Desktop/Ferry"
UNTRUSTED = r"C:\Users\Dell\Desktop\Ayat-Installer"


@pytest.fixture
def home(tmp_path: Path) -> dict[str, str]:
    """A config directory holding the trust map, and the env pointing at it."""
    root = tmp_path / "config"
    root.mkdir()
    (root / ".claude.json").write_text(
        json.dumps(
            {
                "projects": {
                    TRUSTED: {"hasTrustDialogAccepted": True, "allowedTools": []},
                    SLASHED: {"hasTrustDialogAccepted": False},
                    UNTRUSTED + "-other": {"allowedTools": []},
                }
            }
        ),
        encoding="utf-8",
    )
    return {"CLAUDE_CONFIG_DIR": str(root)}


class TestReadingTheTrustMap:
    def test_a_trusted_folder_is_trusted(self, home: dict[str, str]) -> None:
        assert is_trusted(TRUSTED, home) is True

    def test_a_folder_that_is_not_listed_at_all_is_not_trusted(self, home: dict[str, str]) -> None:
        """The case the person actually hit: Ferry filed a conversation under a
        folder Claude Code had never been opened in."""
        assert is_trusted(UNTRUSTED, home) is False

    def test_a_listed_folder_that_never_accepted_is_not_trusted(self, home: dict[str, str]) -> None:
        """Present in the map is not the same as trusted; the flag decides."""
        assert is_trusted(SLASHED, home) is False

    def test_an_entry_without_the_flag_is_not_trusted(self, home: dict[str, str]) -> None:
        assert is_trusted(UNTRUSTED + "-other", home) is False

    def test_the_other_spelling_of_a_trusted_folder_is_not_assumed_trusted(
        self, home: dict[str, str]
    ) -> None:
        """The reason matching is exact.

        ``C:\\Users\\Dell\\Desktop\\Ferry`` is trusted and
        ``C:/Users/Dell/Desktop/Ferry`` is not, in the same real file. A
        normalising comparison would call the second one trusted, suppress the
        warning, and hand the person back the refusal this module exists to
        predict.
        """
        assert is_trusted(TRUSTED, home) is True
        assert is_trusted(SLASHED, home) is False


class TestWhenTheAnswerCannotBeRead:
    """Missing and unreadable are ``None``, never ``False``.

    Warning about a refusal that may never come would teach people to ignore
    the warning that matters.
    """

    def test_no_settings_file_at_all(self, tmp_path: Path) -> None:
        root = tmp_path / "empty"
        root.mkdir()
        assert is_trusted(TRUSTED, {"CLAUDE_CONFIG_DIR": str(root)}) is None

    def test_unparseable_settings(self, tmp_path: Path) -> None:
        root = tmp_path / "broken"
        root.mkdir()
        (root / ".claude.json").write_text("{not json", encoding="utf-8")
        assert is_trusted(TRUSTED, {"CLAUDE_CONFIG_DIR": str(root)}) is None

    def test_settings_without_a_projects_map(self, tmp_path: Path) -> None:
        root = tmp_path / "odd"
        root.mkdir()
        (root / ".claude.json").write_text('{"userID": "x"}', encoding="utf-8")
        assert is_trusted(TRUSTED, {"CLAUDE_CONFIG_DIR": str(root)}) is None


class TestWhereTheMapIsLookedFor:
    def test_an_override_never_falls_back_to_the_real_home_file(self, tmp_path: Path) -> None:
        """The whole point of the override is to stay away from the real one.

        Without this, a run pointed at a spare configuration would answer from
        the person's actual trust map whenever the spare had no copy, and this
        suite would give different results on different machines.
        """
        root = tmp_path / "nothing-here"
        root.mkdir()
        assert config_file({"CLAUDE_CONFIG_DIR": str(root)}) == root / ".claude.json"

    def test_without_an_override_it_is_the_home_file(self) -> None:
        assert config_file({}) == Path.home() / ".claude.json"


class TestWhatThePersonIsTold:
    def test_the_advice_names_the_folder_and_the_thing_to_do(self) -> None:
        said = advice(UNTRUSTED)

        assert UNTRUSTED in said
        # A warning that describes a state without naming the remedy sends the
        # person back to the tool that just refused them.
        assert "Start Claude Code once in that folder" in said

    def test_the_advice_does_not_lean_on_a_dash(self) -> None:
        """House rule: a dash in a menu row or a notice is a missing sentence."""
        assert " - " not in advice(UNTRUSTED)


class TestGrantingTrust:
    """Writing into a file another program owns.

    Offered on screen and never taken silently: the library default is off, so
    only an interactive run that put the question in front of someone reaches
    this code. What is tested here is that when it does run, it is survivable.
    """

    @staticmethod
    def _read(root: Path) -> dict[str, Any]:
        return json.loads((root / ".claude.json").read_text(encoding="utf-8"))

    def test_the_folder_becomes_trusted(self, home: dict[str, str], tmp_path: Path) -> None:
        grant([UNTRUSTED], home, backup_root=tmp_path / "backups")

        assert is_trusted(UNTRUSTED, home) is True

    def test_everything_else_in_the_file_survives(
        self, home: dict[str, str], tmp_path: Path
    ) -> None:
        """A settings file is not a conversation.

        Losing it loses every project's tool permissions and MCP servers along
        with the trust flags, so the write merges rather than replaces.
        """
        root = Path(home["CLAUDE_CONFIG_DIR"])
        before = self._read(root)

        grant([UNTRUSTED], home, backup_root=tmp_path / "backups")

        after = self._read(root)
        assert after["projects"][TRUSTED] == before["projects"][TRUSTED]
        assert after["projects"][SLASHED] == before["projects"][SLASHED]

    def test_an_existing_entry_keeps_its_other_settings(
        self, home: dict[str, str], tmp_path: Path
    ) -> None:
        """Trusting a folder is setting one flag, not rewriting the entry."""
        grant([SLASHED], home, backup_root=tmp_path / "backups")

        entry = self._read(Path(home["CLAUDE_CONFIG_DIR"]))["projects"][SLASHED]
        assert entry["hasTrustDialogAccepted"] is True
        assert is_trusted(SLASHED, home) is True

    def test_the_previous_file_is_saved_before_anything_is_written(
        self, home: dict[str, str], tmp_path: Path
    ) -> None:
        granted = grant([UNTRUSTED], home, backup_root=tmp_path / "backups")

        assert granted.backup.is_file()
        # The copy is the file as it was, not as it became.
        saved = json.loads(granted.backup.read_text(encoding="utf-8"))
        assert UNTRUSTED not in saved["projects"]

    def test_several_folders_at_once(self, home: dict[str, str], tmp_path: Path) -> None:
        """The case that made this worth building: a restore onto a new machine
        lands in many folders and none of them are trusted."""
        others = [UNTRUSTED, UNTRUSTED + "-two", UNTRUSTED + "-three"]

        granted = grant(others, home, backup_root=tmp_path / "backups")

        assert len(granted.folders) == 3
        assert all(is_trusted(folder, home) is True for folder in others)

    def test_a_folder_asked_for_twice_is_granted_once(
        self, home: dict[str, str], tmp_path: Path
    ) -> None:
        granted = grant([UNTRUSTED, UNTRUSTED], home, backup_root=tmp_path / "backups")

        assert granted.folders == (UNTRUSTED,)

    def test_the_file_is_left_valid_json(self, home: dict[str, str], tmp_path: Path) -> None:
        """The write goes to a temporary file and is renamed over the original.

        Writing in place is what produces the config nobody can parse.
        """
        grant([UNTRUSTED], home, backup_root=tmp_path / "backups")

        root = Path(home["CLAUDE_CONFIG_DIR"])
        assert isinstance(self._read(root), dict)
        assert not list(root.glob("*.ferry-tmp"))


class TestWhenTrustCannotBeGranted:
    """Refusing changes nothing. Every failure here leaves the file as it was."""

    def test_no_settings_file_is_refused_rather_than_invented(self, tmp_path: Path) -> None:
        """Ferry knows one key of this file's schema.

        Inventing the rest of a config Claude Code has never written is how you
        produce a file that parses and means nothing.
        """
        root = tmp_path / "absent"
        root.mkdir()

        with pytest.raises(TrustError, match="no Claude Code settings file"):
            grant([UNTRUSTED], {"CLAUDE_CONFIG_DIR": str(root)}, backup_root=tmp_path / "b")

        assert not (root / ".claude.json").exists()

    def test_an_unreadable_file_is_left_alone(self, tmp_path: Path) -> None:
        root = tmp_path / "broken"
        root.mkdir()
        (root / ".claude.json").write_text("{not json", encoding="utf-8")

        with pytest.raises(TrustError, match="cannot read"):
            grant([UNTRUSTED], {"CLAUDE_CONFIG_DIR": str(root)}, backup_root=tmp_path / "b")

        assert (root / ".claude.json").read_text(encoding="utf-8") == "{not json"

    def test_a_file_that_is_not_an_object_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "list"
        root.mkdir()
        (root / ".claude.json").write_text("[1, 2]", encoding="utf-8")

        with pytest.raises(TrustError, match="not the settings file"):
            grant([UNTRUSTED], {"CLAUDE_CONFIG_DIR": str(root)}, backup_root=tmp_path / "b")

    def test_nothing_to_do_is_an_error_not_a_silent_write(
        self, home: dict[str, str], tmp_path: Path
    ) -> None:
        with pytest.raises(TrustError, match="no folders"):
            grant([], home, backup_root=tmp_path / "backups")
