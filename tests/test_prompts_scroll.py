"""A list longer than the screen follows its cursor.

Reported by the human on the backups list - 141 rows: moving past the bottom of
the screen carried the cursor into rows nobody could see, and the list never
moved. Both pickers drew every row and never said where the cursor was, so every
long list in Ferry had it: the backups, the import checklist, the delete
checklist, Inspect's "which conversation should go?".

The frame is built by plain functions, so what matters is testable here without
a terminal: the cursor's row is always drawn, "more above/below" says what is
hidden, and the frame keeps one height while it scrolls.
"""

from __future__ import annotations

from prompt_toolkit.keys import Keys

from ferry.cli.prompts import (
    MIN_ROWS,
    SelectorItem,
    SelectorModel,
    _render,
    _render_multi,
    build_bindings,
)
from ferry.cli.theme import MONO

HEIGHT = 20
"""A small terminal, so a 50-row list cannot fit."""


def items(count: int) -> list[SelectorItem]:
    return [SelectorItem(f"v{index}", f"row {index:03d}") for index in range(count)]


def text(fragments: object) -> str:
    return "".join(fragment[1] for fragment in fragments)  # type: ignore[attr-defined]


def frame(model: SelectorModel, height: int | None = HEIGHT) -> str:
    return text(_render(model, MONO, "Which one?", None, True, height=height))


def checklist(model: SelectorModel, height: int | None = HEIGHT) -> str:
    return text(_render_multi(model, set(), MONO, "Which ones?", height=height))


class TestTheWindow:
    def test_a_list_that_fits_is_drawn_whole(self) -> None:
        model = SelectorModel(items(5))

        assert model.window(10) == (0, 5)

    def test_the_view_stays_put_while_the_cursor_moves_inside_it(self) -> None:
        model = SelectorModel(items(50))
        model.move(4)

        assert model.window(10) == (0, 10)

    def test_moving_past_the_bottom_scrolls_one_row_at_a_time(self) -> None:
        model = SelectorModel(items(50))
        model.window(10)
        for _ in range(12):
            model.move(1)

        start, end = model.window(10)

        assert start <= model.cursor < end
        assert (start, end) == (3, 13)

    def test_moving_back_up_past_the_top_scrolls_up(self) -> None:
        model = SelectorModel(items(50), initial=30)
        assert model.window(10) == (21, 31)
        for _ in range(10):
            model.move(-1)

        start, end = model.window(10)

        assert start == model.cursor == 20
        assert end == 30

    def test_wrapping_from_the_last_row_shows_the_first(self) -> None:
        model = SelectorModel(items(50), initial=49)
        model.window(10)
        model.move(1)

        assert model.window(10) == (0, 10)

    def test_a_filter_starts_the_view_again_from_the_top(self) -> None:
        model = SelectorModel(items(50), initial=40)
        model.window(10)
        model.set_filter("row 04")

        assert model.top == 0

    def test_a_page_stops_at_the_end_rather_than_wrapping(self) -> None:
        model = SelectorModel(items(50), initial=45)
        model.jump(10)
        assert model.cursor == 49
        model.jump(-100)
        assert model.cursor == 0


class TestThePicker:
    def test_every_cursor_position_is_drawn(self) -> None:
        """The report, as a test: walk the whole list and the cursor's row is
        always on screen."""
        model = SelectorModel(items(50))
        for _ in range(50):
            assert f"row {model.cursor:03d}" in frame(model)
            model.move(1)

    def test_it_fits_the_terminal(self) -> None:
        model = SelectorModel(items(50), initial=25)

        assert frame(model).count("\n") <= HEIGHT

    def test_what_is_hidden_is_said(self) -> None:
        model = SelectorModel(items(50))
        top = frame(model)
        model.jump(100)
        bottom = frame(model)

        assert "more below" in top
        assert "more above" not in top
        assert "more above" in bottom
        assert "more below" not in bottom

    def test_the_frame_keeps_one_height_while_it_scrolls(self) -> None:
        """Otherwise everything under the list jumps as the indicators come and go."""
        model = SelectorModel(items(50))
        heights = set()
        for _ in range(50):
            heights.add(frame(model).count("\n"))
            model.move(1)

        assert len(heights) == 1

    def test_the_page_keys_are_named_only_when_there_is_something_to_page(self) -> None:
        assert "pgup/pgdn" in frame(SelectorModel(items(50)))
        assert "pgup/pgdn" not in frame(SelectorModel(items(5)))

    def test_a_short_list_is_drawn_exactly_as_before(self) -> None:
        model = SelectorModel(items(5))

        assert frame(model) == frame(model, height=None)

    def test_a_tiny_terminal_still_shows_a_list(self) -> None:
        model = SelectorModel(items(50))

        drawn = [line for line in frame(model, height=4).splitlines() if "row " in line]

        assert len(drawn) == MIN_ROWS

    def test_the_page_keys_are_bound(self) -> None:
        bound = {binding.keys for binding in build_bindings(SelectorModel(items(3))).bindings}

        for key in (Keys.PageUp, Keys.PageDown, Keys.Home, Keys.End):
            assert (key,) in bound


class TestTheChecklist:
    def test_every_cursor_position_is_drawn(self) -> None:
        model = SelectorModel(items(50))
        for _ in range(50):
            assert f"row {model.cursor:03d}" in checklist(model)
            model.move(1)

    def test_it_fits_the_terminal_and_keeps_one_height(self) -> None:
        model = SelectorModel(items(50))
        heights = set()
        for _ in range(50):
            heights.add(checklist(model).count("\n"))
            model.move(1)

        assert len(heights) == 1
        assert heights.pop() <= HEIGHT

    def test_the_count_of_ticked_rows_counts_the_hidden_ones_too(self) -> None:
        model = SelectorModel(items(50))

        drawn = text(_render_multi(model, {"v49"}, MONO, "Which ones?", height=HEIGHT))

        assert "row 049" not in drawn
        assert "1 of 50 chosen" in drawn
