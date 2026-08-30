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

import pytest

from ferry.adapters.claude_code.trust import advice, config_file, is_trusted

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
