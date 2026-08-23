"""The Copilot delta replayer.

Tested on its own, before any adapter code, because the failure mode is
silent. On the probe machine every session file carried ``requests: []`` in its
snapshot and delivered the real turns as later appends -- so a replayer that
quietly does nothing returns a well-formed conversation with no messages in it
and reports success. Nothing downstream can tell that apart from an empty chat.

The other property under test is that one bad record costs one record. These
files are written by an editor that is still being developed; a kind Ferry has
never seen must not take the conversation down with it.
"""

from __future__ import annotations

import json

from ferry.adapters.copilot.deltas import APPEND, SET, SNAPSHOT, replay, replay_lines


def snap(**fields: object) -> dict[str, object]:
    return {"kind": SNAPSHOT, "v": fields}


def set_(path: list[object], value: object) -> dict[str, object]:
    return {"kind": SET, "k": path, "v": value}


def append(path: list[object], value: object) -> dict[str, object]:
    return {"kind": APPEND, "k": path, "v": value}


# --------------------------------------------------------------------------
# the failure this module exists to prevent
# --------------------------------------------------------------------------


def test_turns_that_arrive_only_as_appends_are_all_found() -> None:
    """The real shape on disk: an empty snapshot, then every turn appended.

    A replayer that reads the snapshot and stops returns a valid-looking
    conversation with zero messages, and no error anywhere.
    """
    result = replay(
        [
            snap(sessionId="s1", requests=[]),
            append(["requests"], [{"requestId": "r1"}]),
            append(["requests"], [{"requestId": "r2"}]),
        ]
    )

    assert [turn["requestId"] for turn in result.document["requests"]] == ["r1", "r2"]
    assert result.clean


def test_order_is_preserved_across_interleaved_deltas() -> None:
    """Appends and sets arrive mixed together; replay is not commutative."""
    result = replay(
        [
            snap(requests=[], inputState={"inputText": ""}),
            append(["requests"], [{"requestId": "r1"}]),
            set_(["inputState", "inputText"], "half-typed"),
            append(["requests"], [{"requestId": "r2"}]),
            set_(["inputState", "inputText"], ""),
        ]
    )

    assert [t["requestId"] for t in result.document["requests"]] == ["r1", "r2"]
    assert result.document["inputState"]["inputText"] == ""


def test_a_later_snapshot_replaces_everything_before_it() -> None:
    result = replay(
        [
            snap(requests=[{"requestId": "old"}]),
            snap(requests=[{"requestId": "new"}]),
        ]
    )

    assert [t["requestId"] for t in result.document["requests"]] == ["new"]


# --------------------------------------------------------------------------
# tolerating a format that is still moving
# --------------------------------------------------------------------------


def test_an_unknown_kind_costs_that_record_and_nothing_else() -> None:
    """Only kinds 0-2 were seen, across 18 records on one machine and one
    build. A VS Code that adds kind 3 must not cost the conversation."""
    result = replay(
        [
            snap(requests=[]),
            {"kind": 3, "k": ["requests"], "v": "who knows"},
            append(["requests"], [{"requestId": "r1"}]),
        ]
    )

    assert [t["requestId"] for t in result.document["requests"]] == ["r1"]
    assert result.unknown_kinds == {3: 1}
    assert not result.clean


def test_a_corrupt_line_costs_that_line_only() -> None:
    lines = [
        json.dumps(snap(requests=[])),
        "{ this is not json",
        json.dumps(append(["requests"], [{"requestId": "r1"}])),
    ]

    result = replay_lines(lines)

    assert [t["requestId"] for t in result.document["requests"]] == ["r1"]
    assert result.unparseable == 1


def test_blank_lines_are_not_counted_as_damage() -> None:
    """A trailing newline is not a lost record, and warning about one would
    put a scare in front of a user whose file is perfectly intact."""
    result = replay_lines([json.dumps(snap(requests=[])), "", "   ", ""])

    assert result.clean
    assert result.skipped == 0


def test_a_path_through_a_non_container_is_refused_rather_than_forced() -> None:
    """Overwriting the string at doc['a'] with a dict to make the path fit
    would silently discard whatever was really there."""
    result = replay([snap(a="a string"), set_(["a", "b"], 1)])

    assert result.document["a"] == "a string"
    assert result.bad_paths == 1


def test_a_delta_with_no_path_is_skipped() -> None:
    result = replay([snap(requests=[]), {"kind": SET, "v": 1}, {"kind": APPEND, "k": [], "v": 1}])

    assert result.bad_paths == 2
    assert result.document == {"requests": []}


def test_a_record_that_is_not_an_object_is_skipped() -> None:
    result = replay([snap(requests=[]), ["not", "a", "record"], None, 7])

    assert result.unparseable == 3


# --------------------------------------------------------------------------
# path handling
# --------------------------------------------------------------------------


def test_appending_to_a_key_that_does_not_exist_yet_creates_the_list() -> None:
    result = replay([snap(sessionId="s1"), append(["requests"], [{"requestId": "r1"}])])

    assert result.document["requests"] == [{"requestId": "r1"}]
    assert result.clean


def test_a_deep_set_creates_the_containers_it_needs() -> None:
    result = replay([snap(), set_(["a", "b", "c"], 1)])

    assert result.document == {"a": {"b": {"c": 1}}}


def test_a_numeric_step_indexes_a_list() -> None:
    """`k` is a path, and a path into a list needs an index. Not observed on
    the sample, but the format allows it and guessing wrong loses a turn."""
    result = replay(
        [
            snap(requests=[{"requestId": "r1", "response": []}]),
            append(["requests", 0, "response"], [{"value": "hello"}]),
        ]
    )

    assert result.document["requests"][0]["response"] == [{"value": "hello"}]
    assert result.clean


def test_an_out_of_range_index_is_skipped_not_created() -> None:
    result = replay([snap(requests=[]), set_(["requests", 5], {"requestId": "r1"})])

    assert result.document["requests"] == []
    assert result.bad_paths == 1


def test_appending_to_something_that_is_not_a_list_is_refused() -> None:
    result = replay([snap(title="a title"), append(["title"], ["more"])])

    assert result.document["title"] == "a title"
    assert result.bad_paths == 1


def test_a_scalar_append_joins_the_list_rather_than_being_dropped() -> None:
    """VS Code always sends a list. If one ever arrives bare, it is still a
    turn, and splitting it into characters would be worse than either."""
    result = replay([snap(requests=[]), append(["requests"], "solo")])

    assert result.document["requests"] == ["solo"]


def test_an_empty_file_replays_to_an_empty_document() -> None:
    result = replay([])

    assert result.document == {}
    assert result.clean


def test_a_file_that_never_had_a_snapshot_still_replays() -> None:
    """Not observed, but a truncated or rotated file would look like this and
    losing the deltas as well as the snapshot helps nobody."""
    result = replay([append(["requests"], [{"requestId": "r1"}])])

    assert result.document["requests"] == [{"requestId": "r1"}]
