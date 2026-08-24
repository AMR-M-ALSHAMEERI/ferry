"""A protobuf wire-format codec that can rewrite one field and change nothing else.

Antigravity stores its conversations as protobuf blobs inside SQLite, and
importing one onto a different machine means rewriting the absolute paths
buried in those blobs. Paths appear at **fourteen or more distinct field
paths** (``PROGRESS.md`` section 4.3), so this is not an edge case in the
format, it is most of what an import does.

A path cannot be replaced with a byte-level search and replace. Every
length-delimited field is preceded by its length, and every message containing
it is preceded by *its* length, so lengthening a path leaves a chain of length
prefixes describing a message that no longer exists. The blob still opens; it
decodes into nonsense. **Rewriting requires decode, edit, re-encode.**

Which normally means a ``.proto`` and generated classes. This module is
deliberately neither, for the one reason a generated decoder cannot answer:
**Ferry does not have Google's schema and never will.** A hand-written
``.proto`` covers the fields that were identified; the rest -- 157 of 7,025
blobs do not parse at all, and no census can prove the other 6,868 were fully
understood -- would survive a round trip only as far as the generated runtime's
unknown-field handling allows. That is not something to bet a user's history
on.

So this codec never claims to understand a message. It keeps **the original
bytes of every field it did not touch** and re-encodes only the chain from the
root down to the one string that changed. An unmodified blob is returned
unexamined, byte-identical by construction rather than by test. The tests
confirm it on real blobs anyway.

That also settles PLAN.md section 8 open question 2 -- ship a compiled
``_pb2.py`` or compile at install time -- by removing the question. There is no
``protoc``, no build step, and no ``protobuf`` dependency. ``schema.proto``
still exists beside this file, as the documentation of what the field numbers
mean.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "MAX_DEPTH",
    "Field",
    "Rewriter",
    "parse",
    "rewrite",
    "strings",
]

VARINT, FIXED64, LEN, START_GROUP, END_GROUP, FIXED32 = range(6)

MAX_DEPTH = 12
"""How far to recurse into nested messages.

Deep enough for the deepest real field path seen (``147.1.21.1.2``, five
levels) with room to spare, and finite so that a blob whose bytes happen to
look like endlessly nested messages cannot exhaust the stack.
"""

_MAX_VARINT_BITS = 64

Path = tuple[int, ...]

Rewriter = Callable[[Path, str], str]
"""Given a field path and the text at it, return the text to store.

Returning the text unchanged means "leave this field alone", and leaving a
field alone means its original bytes are preserved exactly.
"""


@dataclass(frozen=True)
class Field:
    """One field, together with the bytes it arrived as.

    ``encoded`` is the whole field including its tag and, for a
    length-delimited field, its length prefix. It is kept so that a field
    nobody edited can be written back exactly -- including a non-minimally
    encoded varint, which protobuf permits and which any re-encoder working
    from decoded values would silently normalise.
    """

    number: int
    wire: int

    value: bytes
    """The payload only: a length-delimited field's contents, the raw bytes of
    a varint, or the 8 or 4 bytes of a fixed-width field."""

    encoded: bytes


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    """The value, and the offset just past it.

    Raises:
        ValueError: If the varint runs off the end or exceeds 64 bits.
    """
    value = shift = 0
    while i < len(data):
        byte = data[i]
        value |= (byte & 0x7F) << shift
        i += 1
        if not byte & 0x80:
            return value, i
        shift += 7
        if shift >= _MAX_VARINT_BITS:
            raise ValueError("varint longer than 64 bits")
    raise ValueError("varint runs past the end of the buffer")


def _write_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | 0x80 if value else byte)
        if not value:
            return bytes(out)


def parse(data: bytes) -> list[Field] | None:
    """Split a message into its fields, or ``None`` if it is not one.

    Strict on purpose. Every byte must be consumed, no field may run past the
    end, field number zero is invalid, and groups (wire types 3 and 4) are
    refused rather than guessed at -- a deprecated feature Antigravity has
    never been seen to use, and accepting them half-heartedly would mean
    re-encoding something this module cannot faithfully rebuild.

    That strictness is what makes the nested-versus-string decision safe.
    Arbitrary text usually fails one of these rules, so a chunk satisfying all
    of them is genuinely likely to be a message -- and where it is not, the
    caller is still shown both readings.
    """
    fields: list[Field] = []
    i = 0
    size = len(data)
    while i < size:
        start = i
        try:
            key, i = _read_varint(data, i)
        except ValueError:
            return None
        number, wire = key >> 3, key & 7
        if number == 0:
            return None

        if wire == VARINT:
            try:
                _, end = _read_varint(data, i)
            except ValueError:
                return None
            value = data[i:end]
            i = end
        elif wire in (FIXED64, FIXED32):
            width = 8 if wire == FIXED64 else 4
            if i + width > size:
                return None
            value = data[i : i + width]
            i += width
        elif wire == LEN:
            try:
                length, i = _read_varint(data, i)
            except ValueError:
                return None
            if i + length > size:
                return None
            value = data[i : i + length]
            i += length
        else:
            return None

        fields.append(Field(number, wire, value, data[start:i]))

    return fields


def _text(chunk: bytes) -> str | None:
    """``chunk`` as a string, if it plausibly is one.

    Valid UTF-8 is necessary but not sufficient: a short run of arbitrary bytes
    often decodes cleanly, so anything carrying C0 control characters other
    than tab, newline and carriage return is rejected. Real text in this format
    has none, and mistaking packed numbers for a string is how a rewrite would
    corrupt a field it was never asked to touch.
    """
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(character < " " and character not in "\t\n\r" for character in text):
        return None
    return text


def strings(data: bytes, *, _path: Path = (), _depth: int = 0) -> list[tuple[Path, str]]:
    """Every ``(field path, text)`` in the message, nested fields included.

    **A chunk that parses as a nested message is also reported as a string when
    it is valid text**, and both readings come back. That is not hedging. Long
    text frequently parses as a message by coincidence, and returning only the
    structural reading hid every message over a certain length during the M6
    probe -- a bug that produced a confident and wrong conclusion about the
    user's data before its own control caught it (``PROGRESS.md`` ledger #150).
    """
    fields = parse(data)
    if fields is None:
        return []

    found: list[tuple[Path, str]] = []
    for field in fields:
        if field.wire != LEN:
            continue
        here = (*_path, field.number)
        text = _text(field.value)
        if text is not None and text.strip():
            found.append((here, text))
        if _depth < MAX_DEPTH and field.value:
            found.extend(strings(field.value, _path=here, _depth=_depth + 1))
    return found


def rewrite(data: bytes, edit: Rewriter, *, _path: Path = (), _depth: int = 0) -> bytes:
    """The message with ``edit`` applied to every string in it.

    Returns ``data`` itself when nothing changed, which is the common case and
    the guarantee the rest of the adapter rests on: a conversation imported
    without a path remap is written back as the bytes that were read.

    A field is offered to ``edit`` as a string only when the nested reading
    produced no change. Structure wins where both readings are possible,
    because rewriting inside a real nested message is the correct edit, and
    rewriting a message's own bytes as though they were text is not.
    """
    fields = parse(data)
    if fields is None:
        return data

    out: list[bytes] = []
    changed = False
    for field in fields:
        if field.wire != LEN:
            out.append(field.encoded)
            continue

        here = (*_path, field.number)
        payload = field.value

        if _depth < MAX_DEPTH and payload:
            payload = rewrite(field.value, edit, _path=here, _depth=_depth + 1)

        if payload is field.value:
            text = _text(field.value)
            if text is not None:
                replaced = edit(here, text)
                if replaced != text:
                    payload = replaced.encode("utf-8")

        if payload is field.value:
            out.append(field.encoded)
            continue

        changed = True
        out.append(_write_varint(field.number << 3 | LEN) + _write_varint(len(payload)) + payload)

    return b"".join(out) if changed else data
