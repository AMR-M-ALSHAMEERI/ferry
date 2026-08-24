"""Deciding what a recorded path becomes on the machine it is restored to."""

from __future__ import annotations

import pytest

from ferry.adapters.antigravity.remap import PathRemapper, spellings

WINDOWS = PathRemapper(((r"C:\Users\Dell", r"D:\home\amr"),))


class TestSpellings:
    def test_covers_all_four_windows_forms(self) -> None:
        assert set(spellings(r"C:\Users\Dell")) == {
            r"C:\Users\Dell",
            "C:/Users/Dell",
            "file:///c:/Users/Dell",
            "file:///c%3A%5CUsers%5CDell",
        }

    def test_longest_first_so_a_uri_is_matched_before_its_tail(self) -> None:
        """``file:///c:/Users`` contains ``c:/Users``.

        Substituting the shorter form first leaves the ``file:///`` prefix in
        front of an already-remapped path, which is a URI pointing nowhere.
        """
        forms = spellings(r"C:\Users\Dell")
        assert forms == sorted(forms, key=len, reverse=True)

    def test_a_trailing_separator_does_not_change_the_result(self) -> None:
        assert spellings(r"C:\Users\Dell") == spellings("C:/Users/Dell/")

    def test_posix_prefix_has_no_drive_forms(self) -> None:
        assert spellings("/home/amr") == ["file:///home/amr", "/home/amr"]

    def test_empty_prefix_yields_nothing(self) -> None:
        assert spellings("") == []
        assert spellings("/") == []


class TestSubstitution:
    @pytest.mark.parametrize(
        ("before", "after"),
        [
            (r"C:\Users\Dell\Project\a.py", r"D:\home\amr\Project\a.py"),
            ("C:/Users/Dell/Project", "D:/home/amr/Project"),
            ("file:///c:/Users/Dell/x", "file:///d:/home/amr/x"),
            ("file:///c%3A%5CUsers%5CDell%5Cx", "file:///d%3A%5Chome%5Camr%5Cx"),
        ],
    )
    def test_every_spelling_is_remapped_in_its_own_spelling(self, before: str, after: str) -> None:
        assert WINDOWS.text(before) == after

    def test_case_is_ignored_on_a_windows_prefix(self) -> None:
        """The same database writes ``C:\\Users`` and ``c:/Users`` for one folder."""
        assert WINDOWS.text(r"c:\users\dell\p") == r"D:\home\amr\p"

    def test_several_paths_in_one_string_are_all_remapped(self) -> None:
        text = r"moved C:\Users\Dell\a to C:/Users/Dell/b"
        assert WINDOWS.text(text) == r"moved D:\home\amr\a to D:/home/amr/b"

    def test_a_backslash_replacement_is_not_read_as_a_group_reference(self) -> None:
        """``re.sub`` treats a backslash in the replacement as an escape.

        A Windows path is nothing but backslashes, so this is not a corner
        case -- getting it wrong turns ``\\home`` into a backspace character.
        """
        assert "\\" in WINDOWS.text(r"C:\Users\Dell\x")
        assert "\x08" not in WINDOWS.text(r"C:\Users\Dell\x")


class TestLeavesAlone:
    def test_a_longer_name_beginning_with_the_prefix_is_untouched(self) -> None:
        assert WINDOWS.text(r"C:\Users\Dellinger\p") == r"C:\Users\Dellinger\p"

    def test_unrelated_text_is_untouched(self) -> None:
        assert WINDOWS.text("no paths here") == "no paths here"

    def test_a_remapper_with_no_rules_is_inert(self) -> None:
        empty = PathRemapper()
        assert not empty.active
        assert empty.text(r"C:\Users\Dell\x") == r"C:\Users\Dell\x"

    def test_rules_apply_in_order(self) -> None:
        remapper = PathRemapper(
            ((r"C:\a", r"D:\first"), (r"C:\a\b", r"D:\second")),
        )
        assert remapper.text(r"C:\a\b\c") == r"D:\first\b\c"


class TestExactCase:
    def test_case_sensitive_matching_is_reversible(self) -> None:
        """The property the round-trip verification depends on.

        Case-insensitive matching is right for real use and *cannot* round
        trip, because several spellings map onto one. A test that used it would
        report a codec fault that was really the test's own -- which is what
        happened before this distinction existed.
        """
        forward = PathRemapper(((r"C:\Users\Dell", r"D:\home\amr"),), case_insensitive=False)
        backward = PathRemapper(((r"D:\home\amr", r"C:\Users\Dell"),), case_insensitive=False)
        for text in (r"C:\Users\Dell\x", "C:/Users/Dell/x", "file:///c:/Users/Dell/x"):
            assert backward.text(forward.text(text)) == text

    def test_case_sensitive_matching_ignores_the_wrong_case(self) -> None:
        exact = PathRemapper(((r"C:\Users\Dell", r"D:\home\amr"),), case_insensitive=False)
        assert exact.text(r"c:\users\dell\x") == r"c:\users\dell\x"


class TestFieldRewriter:
    def test_ignores_the_field_path(self) -> None:
        """Any field with a path in it is remapped, not only the known ones.

        The set of path-bearing fields was found by inspection and cannot be
        proved complete, so matching on the text is what stops an unseen field
        keeping a path to a directory that does not exist here.
        """
        assert WINDOWS.field((999, 42), r"C:\Users\Dell\x") == r"D:\home\amr\x"
