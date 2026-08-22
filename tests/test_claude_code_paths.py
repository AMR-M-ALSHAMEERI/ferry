"""The project-directory naming rule.

This is the highest-stakes function in the adapter and the one that was blocked
longest: two rules fit the sampled data equally well, and picking the wrong one
misfiles every conversation without raising anything. It was settled twice over
(PROGRESS.md §4.1) — read out of the shipped Claude Code binary, then confirmed
by creating a directory full of punctuation and watching what the tool named it.

The expectations below are written out by hand, not produced by calling
:func:`mangle`. A test that computes its own expectation with the code under
test asserts only that the function is deterministic.
"""

from __future__ import annotations

import ctypes
from functools import reduce
from pathlib import Path

import pytest

from ferry.adapters.claude_code.paths import (
    MAX_PROJECT_DIR_NAME,
    config_root,
    mangle,
    project_dir,
    project_dir_name,
    projects_dir,
    session_files,
    sidecar_dir,
)

BACKSLASH = chr(92)


def windows(*parts: str) -> str:
    return BACKSLASH.join(parts)


@pytest.mark.parametrize(
    ("path_text", "expected"),
    [
        # Observed on the real machine, and the reason the ambiguity existed:
        # neither of these contains a '.' or a '_', so both candidate rules
        # reproduce them.
        (windows("C:", "Users", "Dell", "Desktop", "Ferry"), "C--Users-Dell-Desktop-Ferry"),
        (windows("C:", "Users", "Dell", "Desktop", "CLI AGENT"), "C--Users-Dell-Desktop-CLI-AGENT"),
        # The probe that settled it. Every one of . _ + ~ became a dash, which
        # only the "all non-alphanumerics" rule predicts.
        (
            windows("C:", "Users", "Dell", "Desktop", "ferry-probe", "probe_a.b-c+d~e"),
            "C--Users-Dell-Desktop-ferry-probe-probe-a-b-c-d-e",
        ),
        # Case survives; only the punctuation moves.
        ("/home/Bob/My_Project", "-home-Bob-My-Project"),
        # Runs of punctuation do not collapse — one dash per character.
        ("/a//b", "-a--b"),
        (windows("C:", "", "x"), "C---x"),
        # Non-ASCII letters are not alphanumeric to a JavaScript regex.
        ("/home/bob/café", "-home-bob-caf-"),
    ],
)
def test_mangling_matches_what_claude_code_actually_does(path_text: str, expected: str) -> None:
    assert mangle(path_text) == expected


def reference_hash_suffix(text: str) -> str:
    """An independent implementation of the suffix, written a different way.

    Deliberately not the adapter's loop: ``ctypes.c_int32`` does the 32-bit
    wraparound in hardware terms and :func:`reduce` does the iteration, so a
    mistake in the adapter's hand-rolled masking cannot hide behind an
    identically-mistaken expectation.
    """
    value = reduce(lambda acc, ch: ctypes.c_int32(acc * 31 + ord(ch)).value, text, 0)
    number = abs(value)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if number == 0:
        return "0"
    out = ""
    while number:
        number, remainder = divmod(number, 36)
        out = digits[remainder] + out
    return out


def test_an_over_long_path_is_truncated_and_hashed() -> None:
    long_path = "/home/bob/" + "d/" * 200 + "end"
    name = mangle(long_path)

    head, _, suffix = name.rpartition("-")
    assert len(head) == MAX_PROJECT_DIR_NAME
    assert head == mangle(long_path[:MAX_PROJECT_DIR_NAME])
    assert suffix == reference_hash_suffix(long_path)


def test_the_hash_is_over_the_original_path_not_the_mangled_one() -> None:
    """Two paths that mangle identically must still land in different directories."""
    a = "/home/bob/" + "x/" * 150 + "one_two"
    b = "/home/bob/" + "x/" * 150 + "one.two"

    assert mangle(a)[:MAX_PROJECT_DIR_NAME] == mangle(b)[:MAX_PROJECT_DIR_NAME]
    assert mangle(a) != mangle(b)


def test_the_cutoff_is_inclusive() -> None:
    """A name of exactly the limit is kept whole; one character more is hashed."""
    exact = "/" + "a" * (MAX_PROJECT_DIR_NAME - 1)
    assert len(mangle(exact)) == MAX_PROJECT_DIR_NAME
    assert "-" not in mangle(exact)[1:]

    assert len(mangle(exact + "a")) > MAX_PROJECT_DIR_NAME


def test_the_hash_counts_utf16_units_the_way_javascript_does() -> None:
    """An astral character is two code units to ``charCodeAt`` and one to Python.

    Counting it as one would produce a different hash than the tool's, and the
    conversation would land in a directory Claude Code never looks in.
    """
    astral = "/home/bob/" + "y/" * 150 + "\U0001f600"
    surrogates = "".join(
        chr(unit)
        for unit in memoryview(astral.encode("utf-16-le")).cast("H")  # type: ignore[arg-type]
    )
    suffix = mangle(astral).rpartition("-")[2]

    assert suffix == reference_hash_suffix(surrogates)
    assert suffix != reference_hash_suffix(astral)


def test_config_root_follows_the_environment_override(tmp_path: Path) -> None:
    elsewhere = tmp_path / "somewhere"
    assert config_root({"CLAUDE_CONFIG_DIR": str(elsewhere)}) == elsewhere
    assert projects_dir({"CLAUDE_CONFIG_DIR": str(elsewhere)}) == elsewhere / "projects"


def test_config_root_defaults_to_the_home_directory() -> None:
    assert config_root({}) == Path.home() / ".claude"


def test_the_directory_name_override_needs_the_config_dir_override_too(tmp_path: Path) -> None:
    """Copying the tool's guard, not just its behaviour.

    Claude Code only honours ``CLAUDE_CODE_PROJECT_DIR_NAME`` when the config
    directory is also overridden. An adapter that honoured it unconditionally
    would look somewhere the tool never writes.
    """
    cwd = "/home/bob/proj"
    assert project_dir_name(cwd, {"CLAUDE_CODE_PROJECT_DIR_NAME": "pinned"}) == "-home-bob-proj"

    both = {"CLAUDE_CONFIG_DIR": str(tmp_path), "CLAUDE_CODE_PROJECT_DIR_NAME": "pinned"}
    assert project_dir_name(cwd, both) == "pinned"


@pytest.mark.parametrize("bad", ["has space", "way-too-" + "long" * 20, "con", "LPT1", ""])
def test_an_unusable_directory_name_override_is_ignored(bad: str, tmp_path: Path) -> None:
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path), "CLAUDE_CODE_PROJECT_DIR_NAME": bad}
    assert project_dir_name("/home/bob/proj", env) == "-home-bob-proj"


def test_project_dir_joins_the_two_halves(tmp_path: Path) -> None:
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    assert project_dir("/home/bob/proj", env) == tmp_path / "projects" / "-home-bob-proj"


def test_session_files_are_sorted_and_exclude_everything_else(tmp_path: Path) -> None:
    (tmp_path / "b.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "a.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")
    (tmp_path / "a").mkdir()

    assert [p.name for p in session_files(tmp_path)] == ["a.jsonl", "b.jsonl"]


def test_session_files_of_a_missing_directory_is_empty_not_an_error(tmp_path: Path) -> None:
    assert session_files(tmp_path / "nope") == []


def test_sidecars_sit_under_a_directory_named_for_the_session(tmp_path: Path) -> None:
    session = tmp_path / "11111111-1111-4111-8111-111111111111.jsonl"
    expected = tmp_path / "11111111-1111-4111-8111-111111111111" / "tool-results"
    assert sidecar_dir(session) == expected
