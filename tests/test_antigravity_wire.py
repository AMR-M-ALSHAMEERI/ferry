"""The protobuf codec that has to rewrite a path without disturbing anything else.

This is the highest-risk component in the Antigravity adapter, so it is tested
hardest. The property that matters is not "a rewrite produces valid protobuf" --
corrupt length prefixes usually still parse into *something* -- but that
everything the rewrite was not asked to touch comes back byte for byte.
"""

from __future__ import annotations

import pytest

from ferry.adapters.antigravity import wire
from tests.antigravity_fixture import message, string_field, varint_field


def keep(_path: tuple[int, ...], text: str) -> str:
    return text


class TestParse:
    def test_reads_fields_in_order(self) -> None:
        blob = varint_field(1, 14) + string_field(2, "hello")
        fields = wire.parse(blob)
        assert fields is not None
        assert [(f.number, f.wire) for f in fields] == [(1, 0), (2, 2)]
        assert fields[1].value == b"hello"

    def test_field_keeps_the_bytes_it_arrived_as(self) -> None:
        blob = varint_field(1, 300)
        fields = wire.parse(blob)
        assert fields is not None
        assert fields[0].encoded == blob

    def test_refuses_a_field_running_past_the_end(self) -> None:
        assert wire.parse(b"\x12\x40short") is None

    def test_refuses_groups(self) -> None:
        # Wire type 3 opens a group. Accepting it would mean re-encoding
        # something this codec cannot faithfully rebuild.
        assert wire.parse(b"\x0b\x0c") is None

    def test_refuses_field_number_zero(self) -> None:
        assert wire.parse(b"\x00\x01") is None

    def test_refuses_trailing_rubbish(self) -> None:
        assert wire.parse(string_field(1, "x") + b"\xff") is None

    def test_empty_message_is_a_message(self) -> None:
        assert wire.parse(b"") == []


class TestStrings:
    def test_finds_nested_text(self) -> None:
        blob = message(20, string_field(1, "the answer"))
        assert ((20, 1), "the answer") in wire.strings(blob)

    def test_reports_both_readings_of_an_ambiguous_chunk(self) -> None:
        """A chunk that parses as a message may also be text, and both matter.

        Returning only the structural reading is exactly the bug that hid every
        long message during the Antigravity probe.
        """
        # Text that is also valid protobuf. "2" is a tag byte (field 6, wire
        # 2) and the space after it is a length of 32, so these bytes read
        # equally well as a message and as the printable string they are.
        ambiguous = "2 " + "a" * 32
        blob = string_field(9, ambiguous)
        found = dict(wire.strings(blob))
        assert found[(9,)] == ambiguous
        assert found[(9, 6)] == "a" * 32

    def test_ignores_binary(self) -> None:
        blob = string_field(1, "ok").replace(b"ok", b"\x00\x01")
        assert wire.strings(blob) == []

    def test_ignores_whitespace_only(self) -> None:
        assert wire.strings(string_field(1, "   ")) == []


class TestRewriteLeavesThingsAlone:
    def test_returns_the_same_object_when_nothing_changed(self) -> None:
        blob = varint_field(1, 14) + string_field(2, "hello")
        assert wire.rewrite(blob, keep) is blob

    def test_unparseable_input_comes_back_untouched(self) -> None:
        blob = b"\x0b\x0cnot protobuf"
        assert wire.rewrite(blob, lambda _p, t: "changed") is blob

    def test_non_minimal_varint_survives(self) -> None:
        """Protobuf permits a padded varint, and a re-encoder must not tidy it.

        Normalising it produces a different file for an import that changed
        nothing, which is indistinguishable from a corrupted one at a glance.
        """
        padded = b"\x08\x8e\x80\x80\x00"  # field 1, value 14, four bytes not one
        blob = padded + string_field(2, "x")
        assert wire.parse(blob) is not None
        assert wire.rewrite(blob, keep) is blob

    def test_untouched_neighbours_keep_their_bytes(self) -> None:
        blob = string_field(1, "keep me") + string_field(2, "change me")
        after = wire.rewrite(blob, lambda _p, t: "NEW" if t == "change me" else t)
        assert after.startswith(string_field(1, "keep me"))


class TestRewriteChanges:
    def test_shorter_text_shrinks_the_length_prefix(self) -> None:
        after = wire.rewrite(string_field(1, "aaaaaaaa"), lambda _p, _t: "b")
        assert after == string_field(1, "b")

    def test_longer_text_grows_every_enclosing_length(self) -> None:
        blob = message(3, message(4, string_field(5, "a")))
        after = wire.rewrite(blob, lambda _p, _t: "a" * 200)
        assert wire.parse(after) is not None
        assert dict(wire.strings(after))[(3, 4, 5)] == "a" * 200

    def test_the_edit_sees_the_full_field_path(self) -> None:
        seen: list[tuple[int, ...]] = []

        def note(path: tuple[int, ...], text: str) -> str:
            seen.append(path)
            return text

        wire.rewrite(message(7, message(8, string_field(9, "deep"))), note)
        assert (7, 8, 9) in seen

    def test_structure_wins_over_text_where_both_are_possible(self) -> None:
        """A container edited through its children is not also edited as text.

        Doing both would apply the substitution twice and write the second
        result over a message that no longer matched its length prefixes.
        """
        blob = message(2, string_field(1, "old"))
        after = wire.rewrite(blob, lambda _p, t: t.replace("old", "new"))
        assert dict(wire.strings(after))[(2, 1)] == "new"
        assert wire.parse(after) is not None

    @pytest.mark.parametrize("size", [1, 2, 500, 20_000])
    def test_round_trips_at_every_length_prefix_width(self, size: int) -> None:
        """Length prefixes are varints, so their width changes with the payload.

        Growing a field across one of those boundaries is where a re-encoder
        that assumed a fixed-width prefix would corrupt the message.
        """
        blob = message(1, string_field(2, "x"))
        grown = wire.rewrite(blob, lambda _p, _t: "y" * size)
        assert dict(wire.strings(grown))[(1, 2)] == "y" * size
        back = wire.rewrite(grown, lambda _p, _t: "x")
        assert back == blob


class TestDepth:
    @staticmethod
    def _buried(levels: int) -> bytes:
        blob = string_field(1, "bottom")
        for number in range(levels):
            blob = message(number + 2, blob)
        return blob

    def test_finds_text_within_the_limit(self) -> None:
        assert "bottom" in dict(wire.strings(self._buried(wire.MAX_DEPTH - 1))).values()

    def test_does_not_descend_past_the_limit(self) -> None:
        """Text buried deeper than the limit is not found -- and not corrupted.

        A real field path is five levels deep, so the limit is generous, but it
        is a limit and this records what happens at it: the codec stops looking
        rather than recursing without end, and a rewrite it could not reach
        leaves the bytes exactly as they were.
        """
        deep = self._buried(wire.MAX_DEPTH + 4)
        assert "bottom" not in dict(wire.strings(deep)).values()
        assert wire.rewrite(deep, lambda _p, _t: "REPLACED") is deep
