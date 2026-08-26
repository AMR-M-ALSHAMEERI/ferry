"""Layer-2 self-check for M7c -- Compact, without an LLM.

Contract (PLAN.md 6.8.2): numbered PASS/FAIL/SKIP lines, non-zero exit on any
failure, runs against **real data on this machine**, never prints conversation
content, and never modifies user data.

Compact is the one feature in Ferry whose central claim a person cannot check
by looking at the output. *Nothing here was invented* is exactly the sort of
statement a fabricated sentence about your own conversation would not
contradict -- it would read as true. So the claim is tested mechanically, on
every real conversation on this machine, at every shape and every length.

Four checks carry the weight:

- **Check 4, the quotation property.** Every line of every document that claims
  to be a quotation is looked for in the conversation it came from. This is the
  check that makes the feature honest rather than merely careful.
- **Check 5, determinism.** The same conversation twice, byte for byte. An
  LLM Compact could not have had this check at all.
- **Checks 6 and 7, the targets.** Under two seconds for the largest
  conversation here, and Standard under two thousand words. Both were **missed
  by the first implementation** -- seven seconds and 438 kilobytes -- and both
  are here because measuring is what found that.

Check 9 is the mandatory one: the export that feeds all of this is fingerprinted
before and after, asserting none of it moved a byte of real data.

**Prints no conversation content.** Counts, ratios, seconds, and the names of
the tools.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote
from uuid import UUID

from ferry.adapters.antigravity import AntigravityAdapter
from ferry.adapters.base import Adapter
from ferry.adapters.claude_code import ClaudeCodeAdapter
from ferry.adapters.codex import CodexAdapter
from ferry.adapters.copilot import CopilotAdapter
from ferry.compact import LENGTHS, SHAPES, compact, quotations
from ferry.core import Bundle, sha256_file
from ferry.ucs import Conversation

MAX_SECONDS = 2.0
MAX_STANDARD_WORDS = 2000
MAX_RATIO = 0.20
"""A conversation of any size must compact to a fifth of itself or less.

Applied only above :data:`RATIO_FLOOR`. Below it there is nothing to compact --
a fifteen-message conversation is mostly the words people typed, and those are
kept on purpose. Holding a tiny conversation to a ratio would be measuring the
absence of tool output and calling it a failure.
"""

RATIO_FLOOR = 200 * 1024


@dataclass
class Result:
    ok: bool | None
    detail: str


@dataclass
class State:
    workspace: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="ferry-m7c-")))
    bundle: Bundle | None = None
    conversations: list[Conversation] = field(default_factory=list)
    fingerprint_before: dict[str, str] = field(default_factory=dict)
    documents: dict[UUID, str] = field(default_factory=dict)
    slowest: float = 0.0

    @property
    def root(self) -> Path:
        return self.workspace / "bundle"

    def cleanup(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)


def _adapters() -> list[Adapter]:
    return [ClaudeCodeAdapter(), CodexAdapter(), CopilotAdapter(), AntigravityAdapter()]


def _fingerprint(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _flat(text: str) -> str:
    return " ".join(text.split())


def _strings(value: object) -> list[str]:
    """Every string inside a tool call's arguments or a result's payload.

    ``str(a_dict)`` is not the conversation -- it is Python's rendering of it,
    with newlines spelled as backslash-n. A command that really was two lines
    would never be found in it. Walking the structure compares what the
    conversation holds rather than how Python prints it.
    """
    if isinstance(value, str):
        # The decoded spelling counts as the same string. Copilot records a
        # file as `file:///c%3A/Users/...` and the catalogue renders it as
        # `c:/Users/...` -- the same path, re-spelled, exactly as re-wrapping
        # whitespace is the same sentence re-spelled. **These two are the only
        # transformations this comparison allows**, and both are reversible and
        # lossless. Anything else counts as invented, which is the point.
        decoded = unquote(value)
        return [value] if decoded == value else [value, decoded]
    if isinstance(value, dict):
        return [part for item in value.values() for part in _strings(item)]
    if isinstance(value, list | tuple):
        return [part for item in value for part in _strings(item)]
    return []


def _source_text(conversation: Conversation) -> str:
    """Everything a conversation contains, as one flat run of words.

    Whitespace is collapsed on both sides of the comparison, because a
    quotation is the same words in the same order -- a document that preserved
    the source's line wrapping would be unreadable, and re-wrapping a sentence
    is not inventing one.
    """
    parts: list[str] = []
    for message in conversation.messages:
        for block in message.content:
            if block.type in ("text", "thinking"):
                parts.append(block.text)
            elif block.type == "tool_use":
                parts.extend(_strings(block.input))
            elif block.type == "tool_result":
                parts.extend(_strings(block.output))
    return _flat(" ".join(parts))


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------


def check_export(state: State) -> Result:
    """A real bundle, from every tool installed here."""
    found = 0
    tools: list[str] = []
    for adapter in _adapters():
        count = sum(1 for event in adapter.export(state.root) if event.kind == "progress")
        if count:
            tools.append(adapter.display_name)
            found += count
    if not found:
        return Result(None, "no conversations on this machine")
    state.fingerprint_before = _fingerprint(state.root)
    return Result(True, f"{found} conversations from {', '.join(tools)}")


def check_loaded(state: State) -> Result:
    if not state.fingerprint_before:
        return Result(None, "nothing exported")
    state.bundle = Bundle.open(state.root)
    state.conversations = [
        state.bundle.load_conversation(found) for found in state.bundle.list_conversations()
    ]
    total = sum(len(c.messages) for c in state.conversations)
    return Result(bool(state.conversations), f"{len(state.conversations)} loaded, {total} messages")


def check_every_conversation_compacts(state: State) -> Result:
    """No tool, no shape and no length may raise."""
    if not state.conversations:
        return Result(None, "nothing exported")
    for conversation in state.conversations:
        started = time.perf_counter()
        state.documents[conversation.id] = compact(conversation, version="selfcheck")
        state.slowest = max(state.slowest, time.perf_counter() - started)
        for shape in SHAPES:
            for length in LENGTHS:
                compact(conversation, shape=shape, length=length, version="selfcheck")
    combinations = len(state.conversations) * len(SHAPES) * len(LENGTHS)
    return Result(True, f"{combinations} documents, none raised")


def check_the_quotation_property(state: State) -> Result:
    """Every line claiming to be a quotation is one.

    The check the whole feature rests on. Run at every shape and length,
    because a section that only appears at Full is still a section that could
    make something up.
    """
    if not state.documents:
        return Result(None, "nothing exported")
    checked = 0
    for conversation in state.conversations:
        source = _source_text(conversation)
        for shape in SHAPES:
            for length in LENGTHS:
                document = compact(conversation, shape=shape, length=length, version="selfcheck")
                for quotation in quotations(document):
                    wanted = _flat(quotation).removesuffix("...")
                    checked += 1
                    if wanted and wanted not in source:
                        # The offending text is NOT printed -- it is a piece of
                        # a real conversation. Where it came from is enough to
                        # find it with the test suite.
                        return Result(
                            False,
                            f"a {shape}/{length} document from {conversation.source_tool} "
                            f"quoted {len(wanted)} characters that are not in it",
                        )
    return Result(True, f"{checked} quotations, all found in their source")


def check_determinism(state: State) -> Result:
    if not state.documents:
        return Result(None, "nothing exported")
    for conversation in state.conversations:
        if compact(conversation, version="selfcheck") != state.documents[conversation.id]:
            return Result(False, f"{conversation.source_tool} rendered differently twice")
    return Result(True, f"{len(state.conversations)} conversations, byte-identical twice")


def check_latency(state: State) -> Result:
    if not state.documents:
        return Result(None, "nothing exported")
    ok = state.slowest <= MAX_SECONDS
    return Result(ok, f"slowest {state.slowest:.2f}s, budget {MAX_SECONDS:.0f}s")


def check_size(state: State) -> Result:
    """Standard is meant to be about a page, whatever the conversation."""
    if not state.documents:
        return Result(None, "nothing exported")
    worst = 0
    for document in state.documents.values():
        worst = max(worst, len(document.split()))
    ok = worst <= MAX_STANDARD_WORDS
    return Result(ok, f"longest standard {worst} words, budget {MAX_STANDARD_WORDS}")


def check_compression(state: State) -> Result:
    """A large conversation must lose at least four fifths of itself."""
    if not state.documents:
        return Result(None, "nothing exported")
    measured = 0
    worst = 0.0
    for conversation in state.conversations:
        original = len(conversation.model_dump_json())
        if original < RATIO_FLOOR:
            continue
        measured += 1
        worst = max(worst, len(state.documents[conversation.id]) / original)
    if not measured:
        return Result(None, f"no conversation here is over {RATIO_FLOOR // 1024} KB")
    ok = worst <= MAX_RATIO
    return Result(ok, f"{measured} measured, worst {worst:.2%} of the original")


def check_nothing_moved(state: State) -> Result:
    """The mandatory one. Compacting reads; it must not write."""
    if not state.fingerprint_before:
        return Result(None, "nothing exported")
    after = _fingerprint(state.root)
    if after != state.fingerprint_before:
        changed = set(after) ^ set(state.fingerprint_before)
        return Result(False, f"{len(changed) or 'some'} files differ")
    return Result(True, f"all {len(after)} checksums match")


CHECKS = [
    ("export a real bundle", check_export),
    ("load every conversation in it", check_loaded),
    ("every tool, shape and length compacts", check_every_conversation_compacts),
    ("every quotation is really a quotation", check_the_quotation_property),
    ("the same conversation renders the same bytes", check_determinism),
    ("fast enough to watch", check_latency),
    ("standard is about a page", check_size),
    ("a large conversation loses four fifths of itself", check_compression),
    ("the bundle it read is untouched", check_nothing_moved),
]


def main() -> int:
    state = State()
    print("Ferry self-check - M7c (Compact, without an LLM)")
    passed = failed = skipped = 0
    try:
        for number, (label, run) in enumerate(CHECKS, start=1):
            try:
                result = run(state)
            except Exception as exc:  # noqa: BLE001 - a crashed check is a failed check
                result = Result(False, f"{exc.__class__.__name__}: {exc}")
            if result.ok is None:
                status, skipped = "SKIP", skipped + 1
            elif result.ok:
                status, passed = "PASS", passed + 1
            else:
                status, failed = "FAIL", failed + 1
            dots = "." * max(3, 52 - len(label))
            print(f"  {number}. {label} {dots} {status} ({result.detail})")
    finally:
        state.cleanup()

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
