"""Cross-tool migration: which pairs work, what they cost, and what is refused.

The measured facts these tests encode come from ``spikes/probe_cross_tool.py``,
run against the four real stores: every ordered pair imported into a scratch
store and then exported back out, because *the import reported success* and
*the conversation is really there* are different claims.

The most important test here is
:meth:`TestTheFalseWrite.test_a_foreign_transcript_is_not_installed_as_a_database`.
It is a regression test for the defect that probe found -- and the reason the
probe existed at all.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from ferry.adapters.antigravity import AntigravityAdapter
from ferry.adapters.antigravity import paths as ag_paths
from ferry.adapters.base import ImportOptions
from ferry.adapters.claude_code import ClaudeCodeAdapter
from ferry.core import Bundle, Manifest, SourceMachine
from ferry.core.compat import CEILING, assess, pair, refusal
from ferry.ucs import (
    Conversation,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Workspace,
)

TOOLS = ("claude-code", "codex", "copilot", "antigravity")


def manifest() -> Manifest:
    return Manifest(
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
        created_by="ferry test",
        source_machine=SourceMachine(hostname="test", os="win32", user_home="/home/test"),
    )


def conversation(tool: str = "claude-code", **blocks: int) -> Conversation:
    content: list[object] = [TextBlock(text="Make the seal step atomic.")]
    for _ in range(blocks.get("signed", 0)):
        content.append(ThinkingBlock(text="Checking the rename.", signature="vendor-issued"))
    for _ in range(blocks.get("unsigned", 0)):
        content.append(ThinkingBlock(text="Checking the rename."))
    for index in range(blocks.get("calls", 0)):
        content.append(ToolUseBlock(name="Bash", input={"command": "pytest"}, id=f"c{index}"))
        content.append(ToolResultBlock(tool_use_id=f"c{index}", output="ok"))
    return Conversation(
        id=uuid4(),
        source_tool=tool,  # type: ignore[arg-type]
        title="A session",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        updated_at=datetime(2026, 8, 1, tzinfo=UTC),
        workspace=Workspace(name="Ferry", original_path="C:/Users/test/Ferry"),
        messages=[Message(role="user", content=content)],  # type: ignore[arg-type]
    )


class TestTheMatrix:
    """The table is measured. These assert what was measured, not what is hoped."""

    @pytest.mark.parametrize("tool", TOOLS)
    def test_a_tool_importing_its_own_conversation_is_not_a_migration(self, tool: str) -> None:
        assert pair(tool, tool).support == "native"
        assert refusal(tool, tool) == ""

    @pytest.mark.parametrize("source", [t for t in TOOLS if t != "claude-code"])
    def test_claude_code_accepts_all_three_others(self, source: str) -> None:
        """Measured: 3 of 3, every message, 100% of the words read back."""
        assert pair(source, "claude-code").support == "supported"
        assert refusal(source, "claude-code") == ""

    @pytest.mark.parametrize("source", [t for t in TOOLS if t != "copilot"])
    def test_copilot_accepts_all_three_others(self, source: str) -> None:
        """Measured at M7b.2 Phase 2.

        The document is built rather than replayed, to the smallest shape VS
        Code was proven to accept: it listed the conversation, read the title
        out of the document itself, and rendered the reply (#203, #204).
        """
        assert pair(source, "copilot").support == "supported"
        assert refusal(source, "copilot") == ""

    @pytest.mark.parametrize("target", ["codex", "antigravity"])
    def test_every_other_target_is_refused_with_a_reason(self, target: str) -> None:
        found = pair("claude-code", target)
        assert found.support == "unsupported"
        # A refusal without a reason is the failure mode this table exists to
        # avoid: it tells someone their conversation cannot move and leaves
        # them with nowhere to go and nothing to read.
        assert len(found.reason) > 40
        assert refusal("claude-code", target) == found.reason

    def test_an_unknown_target_is_refused_rather_than_assumed_to_work(self) -> None:
        found = pair("claude-code", "some-future-tool")
        assert found.support == "unsupported"
        assert "some-future-tool" in found.reason


class TestWhatAConversionCosts:
    def test_a_native_import_loses_nothing_and_says_nothing(self) -> None:
        """A restore is not a conversion, and should not be dressed as one."""
        loss = assess(conversation("claude-code", calls=3), "claude-code")
        assert loss.notes == ()
        assert loss.degraded == 0
        assert loss.lossy is False

    def test_signatures_are_counted_because_they_cannot_be_reissued(self) -> None:
        loss = assess(conversation("codex", signed=2, unsigned=5), "claude-code")
        signature_notes = [n for n in loss.notes if "signature" in n]
        assert len(signature_notes) == 1
        # Two, not seven: an unsigned thinking block loses nothing by moving.
        assert signature_notes[0].startswith("2 thinking blocks")

    def test_tool_calls_are_counted_as_pairs_not_as_calls(self) -> None:
        loss = assess(conversation("codex", calls=3), "claude-code")
        calls = [n for n in loss.notes if "tool calls" in n]
        assert calls[0].startswith("6 tool calls")

    def test_the_ceiling_is_stated_on_every_conversion(self) -> None:
        """The honest-scope claim, said wherever a conversion is offered.

        It is the sentence someone needs *before* spending an evening
        converting a year of history: what they get is a readable transcript,
        not a session that can be continued.
        """
        for source in TOOLS:
            if source == "claude-code":
                continue
            assert CEILING in assess(conversation(source), "claude-code").notes

    def test_attribution_is_never_rewritten(self) -> None:
        loss = assess(conversation("codex"), "claude-code")
        assert any("stays attributed to codex" in note for note in loss.notes)


class TestTheFalseWrite:
    """The defect the M7b probe found, kept fixed.

    Handed a Claude Code conversation with ``allow_cross_tool=True``, the
    Antigravity importer copied its 1.6 MB **JSONL transcript** into
    ``conversations/<uuid>.db``, ran the path remapper over it without
    complaint, and reported ``1 of 1 imported``. Nothing was readable
    afterwards. The guard asked *is there a file* where it had to ask *is it a
    database*.
    """

    @staticmethod
    def bundle_with(tmp_path: Path, item: Conversation, raw: bytes) -> Path:
        root = tmp_path / "bundle"
        made = Bundle.create(root, manifest())
        made.add_conversation(item)
        # source_raw is written by whichever adapter exported the conversation,
        # so its presence says nothing at all about its format.
        sidecar = tmp_path / "raw.bin"
        sidecar.write_bytes(raw)
        made.add_source_raw(item.id, sidecar)
        return root

    def test_a_foreign_transcript_is_not_installed_as_a_database(self, tmp_path: Path) -> None:
        item = conversation("claude-code", calls=2)
        root = self.bundle_with(tmp_path, item, b'{"type":"user","message":"hello"}\n' * 50)
        store = {ag_paths.DATA_DIR_ENV: str(tmp_path / "store")}

        events = list(AntigravityAdapter(store).import_(root, ImportOptions(allow_cross_tool=True)))

        assert sum(1 for e in events if e.kind == "progress") == 0
        assert not (ag_paths.conversations_dir(store) / f"{item.id}.db").exists()

    def test_a_truncated_database_is_refused_even_from_antigravity_itself(
        self, tmp_path: Path
    ) -> None:
        """The check is not about foreignness, it is about the bytes.

        A native conversation whose database was half-copied is the same
        situation: a file that is not a database must not be installed as one,
        whichever tool it claims to have come from.
        """
        item = conversation("antigravity")
        root = self.bundle_with(tmp_path, item, b"SQLite format")  # one byte short
        store = {ag_paths.DATA_DIR_ENV: str(tmp_path / "store")}

        events = list(AntigravityAdapter(store).import_(root, ImportOptions()))

        assert sum(1 for e in events if e.kind == "progress") == 0
        assert any("not a SQLite database" in e.message for e in events if e.kind == "skipped")
        assert not (ag_paths.conversations_dir(store) / f"{item.id}.db").exists()

    def test_the_refusal_says_what_is_wrong_rather_than_only_that_it_failed(
        self, tmp_path: Path
    ) -> None:
        item = conversation("codex")
        root = self.bundle_with(tmp_path, item, b"not a database at all")
        store = {ag_paths.DATA_DIR_ENV: str(tmp_path / "store")}

        events = list(AntigravityAdapter(store).import_(root, ImportOptions(allow_cross_tool=True)))
        skipped = [e.message for e in events if e.kind == "skipped"]

        assert skipped
        # Refused by the table before the bytes are ever read: the person asked
        # for something that cannot work, and is told why rather than being
        # handed the failure of the attempt.
        assert "original SQLite database" in skipped[0]


class TestAskingBeforeWriting:
    """``allow_cross_tool`` is a request, not an override."""

    @pytest.mark.parametrize("source", ["codex", "copilot", "antigravity"])
    def test_a_foreign_conversation_is_skipped_without_the_flag(
        self, source: str, tmp_path: Path
    ) -> None:
        from ferry.adapters.claude_code import ClaudeCodeAdapter

        item = conversation(source)
        root = tmp_path / "bundle"
        Bundle.create(root, manifest()).add_conversation(item)
        store = {"CLAUDE_CONFIG_DIR": str(tmp_path / "store")}

        events = list(ClaudeCodeAdapter(store).import_(root, ImportOptions()))

        assert sum(1 for e in events if e.kind == "progress") == 0
        assert any("must be asked for explicitly" in e.message for e in events)

    @pytest.mark.parametrize("source", ["codex", "copilot", "antigravity"])
    def test_and_written_with_it(self, source: str, tmp_path: Path) -> None:
        from ferry.adapters.claude_code import ClaudeCodeAdapter

        item = conversation(source, calls=1)
        root = tmp_path / "bundle"
        Bundle.create(root, manifest()).add_conversation(item)
        store = {"CLAUDE_CONFIG_DIR": str(tmp_path / "store")}

        events = list(ClaudeCodeAdapter(store).import_(root, ImportOptions(allow_cross_tool=True)))

        assert sum(1 for e in events if e.kind == "progress") == 1

    def test_a_dry_run_prints_the_cost_not_only_the_size(self, tmp_path: Path) -> None:
        """PLAN M7b: a dry run must show the conversion notes it would record.

        "Would write 400 KB" is equally true of a conversion that drops every
        tool result, and is not something a person can decide on.
        """
        from ferry.adapters.claude_code import ClaudeCodeAdapter

        item = conversation("codex", signed=1, calls=4)
        root = tmp_path / "bundle"
        Bundle.create(root, manifest()).add_conversation(item)
        store = {"CLAUDE_CONFIG_DIR": str(tmp_path / "store")}

        events = list(
            ClaudeCodeAdapter(store).import_(
                root, ImportOptions(dry_run=True, allow_cross_tool=True)
            )
        )
        said = " ".join(e.message for e in events)

        assert "signature" in said
        assert CEILING in said
        assert "blocks degraded" in said
        # And nothing was written.
        assert not list((tmp_path / "store").rglob("*.jsonl"))


class TestWhatWasWrittenSaysWhereItCameFrom:
    def test_provenance_records_the_notes_the_person_was_shown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PLAN §3.2: never present a converted conversation as native.

        The notes recorded are the assessment's *and* the rebuild's. A
        provenance block saying less than the confirmation screen would be the
        more durable of the two documents disagreeing with the one someone
        actually read.
        """
        from ferry.adapters.claude_code.adapter import TOOL
        from ferry.core import provenance as provenance_store
        from ferry.core.compat import assess as assess_loss

        item = conversation("codex", signed=2, calls=3)
        loss = assess_loss(item, TOOL)

        assert loss.lossy
        assert loss.degraded == 8
        assert len(loss.notes) >= 4

        # Everything above is about `assess`. **This test used to stop there**,
        # under a name promising it checked provenance, while the provenance
        # block was built on import and then thrown away -- which is how a
        # broken promise survived a green suite. The record is now read back
        # from where it durably lives and compared against the notes the
        # screen would have shown.
        root = tmp_path / "bundle"
        made = Bundle.create(root, manifest())
        made.add_conversation(item)
        monkeypatch.setenv(provenance_store.ROOT_ENV, str(tmp_path / "ferry"))
        env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "store")}
        list(ClaudeCodeAdapter(env).import_(root, ImportOptions(allow_cross_tool=True)))

        recorded = provenance_store.recall(TOOL, item.id)
        assert recorded is not None, "the conversion left no record of itself"
        assert recorded.original_tool == "codex"
        for note in loss.notes:
            assert note in recorded.conversion_notes, f"the record omits: {note}"


class TestAConvertedCallIsNotWrittenAsACall:
    """The line between a converted transcript and a forgery.

    Found by rehearsing A7b.4 against real data, not by a failing test: a
    conversation converted into Claude Code carried a native ``tool_use`` block
    named after an **Antigravity** tool, complete with an id and an input, in
    the exact shape of a call Claude Code had made. Nothing had run. The screen
    shown before the person agreed said the opposite -- *kept as readable text,
    not as tool calls Claude Code can run* -- and of those two documents the
    transcript is the one that lasts.

    Continue mode had always flattened calls to text. Archive mode had not, and
    the synthesis path could not tell the two situations apart: rebuilding a
    *Claude Code* conversation from UCS should write its calls back as calls,
    because that is what they were.
    """

    @staticmethod
    def _written(tmp_path: Path, item: Conversation) -> list[dict]:
        root = tmp_path / "bundle"
        made = Bundle.create(root, manifest())
        made.add_conversation(item)
        store = {"CLAUDE_CONFIG_DIR": str(tmp_path / "store")}
        list(ClaudeCodeAdapter(store).import_(root, ImportOptions(allow_cross_tool=True)))
        found = sorted((tmp_path / "store").rglob("*.jsonl"))
        assert found, "nothing was written"
        return [
            json.loads(line)
            for path in found
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    @staticmethod
    def _blocks(records: list[dict]) -> list[dict]:
        return [
            block
            for record in records
            for block in (record.get("message") or {}).get("content") or []
            if isinstance(block, dict)
        ]

    def test_a_foreign_call_becomes_text(self, tmp_path: Path) -> None:
        item = conversation("antigravity", calls=2)

        blocks = self._blocks(self._written(tmp_path, item))

        assert not [b for b in blocks if b.get("type") == "tool_use"], (
            "a call another assistant made was written as one Claude Code made"
        )
        assert not [b for b in blocks if b.get("type") == "tool_result"]
        assert [b for b in blocks if b.get("type") == "text"]

    def test_a_restore_still_writes_a_call_as_a_call(self, tmp_path: Path) -> None:
        """The half that must not change.

        Rebuilding a Claude Code conversation from UCS is not a conversion. Its
        calls were made by Claude Code, to tools Claude Code has, and flattening
        them would be losing detail for no reason at all.
        """
        item = conversation("claude-code", calls=2)

        blocks = self._blocks(self._written(tmp_path, item))

        assert [b for b in blocks if b.get("type") == "tool_use"]

    def test_the_import_says_it_did_this(self, tmp_path: Path) -> None:
        """Said out loud, not done quietly. A conversion that silently changes
        the shape of the transcript is the same failure in a smaller place."""
        item = conversation("codex", calls=1)
        root = tmp_path / "bundle"
        made = Bundle.create(root, manifest())
        made.add_conversation(item)
        store = {"CLAUDE_CONFIG_DIR": str(tmp_path / "store")}

        events = list(ClaudeCodeAdapter(store).import_(root, ImportOptions(allow_cross_tool=True)))

        assert any("readable text" in e.message for e in events if e.kind == "warning")
