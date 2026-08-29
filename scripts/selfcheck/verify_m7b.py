"""Layer-2 self-check for M7b -- cross-tool migration.

Contract (PLAN.md 6.8.2): numbered PASS/FAIL/SKIP lines, non-zero exit on any
failure, runs against **real data on this machine**, never prints conversation
content, and never modifies user data.

Every target here is redirected by environment variable into a temporary
directory. **Nothing is written to a real store**, and check 8 fingerprints the
exported bundle before and after to prove the reads did not move a byte either.

Two checks carry the weight:

- **Check 3, the round trip.** A conversation is imported into a scratch target
  and then *exported back out*, and the words are compared. "The import
  reported success" and "the conversation is really there" are different
  claims, and the first one is what the M7b probe caught being wrong.
- **Check 4, the false write.** The defect that probe found: handed a Claude
  Code conversation, the Antigravity importer copied its JSONL transcript to
  ``conversations/<uuid>.db`` and reported ``1 of 1 imported``. A success
  message over an empty result is worse than an error, because nobody goes
  looking.

**Prints no conversation content.** Counts, ratios, tool names and verdicts.
"""

from __future__ import annotations

import platform
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ferry import __version__
from ferry.adapters.antigravity import AntigravityAdapter
from ferry.adapters.antigravity import paths as ag_paths
from ferry.adapters.base import Adapter, ImportOptions
from ferry.adapters.claude_code import ClaudeCodeAdapter
from ferry.adapters.codex import CodexAdapter
from ferry.adapters.copilot import CopilotAdapter
from ferry.core import Bundle, Manifest, SourceMachine, sha256_file
from ferry.core.compat import CEILING, assess, pair
from ferry.ucs import Conversation

TOOLS = ("claude-code", "codex", "copilot", "antigravity")

REDIRECT: dict[str, tuple[str, ...]] = {
    "claude-code": ("CLAUDE_CONFIG_DIR",),
    "codex": ("CODEX_HOME",),
    "copilot": ("FERRY_VSCODE_USER_DIR",),
    "antigravity": (ag_paths.DATA_DIR_ENV, ag_paths.INSTALL_DIR_ENV),
}


@dataclass
class Result:
    ok: bool | None
    detail: str


@dataclass
class State:
    workspace: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="ferry-m7b-")))
    conversations: list[Conversation] = field(default_factory=list)
    smallest: dict[str, Conversation] = field(default_factory=dict)
    bundle: Bundle | None = None
    fingerprint_before: dict[str, str] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return self.workspace / "bundle"

    def cleanup(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)


def _adapters() -> list[Adapter]:
    return [ClaudeCodeAdapter(), CodexAdapter(), CopilotAdapter(), AntigravityAdapter()]


def _build(tool: str, env: dict[str, str] | None) -> Adapter:
    if tool == "claude-code":
        return ClaudeCodeAdapter(env)
    if tool == "codex":
        return CodexAdapter(env)
    if tool == "copilot":
        return CopilotAdapter(env)
    return AntigravityAdapter(env)


def _scratch(tool: str, where: Path) -> dict[str, str]:
    where.mkdir(parents=True, exist_ok=True)
    return {name: str(where) for name in REDIRECT[tool]}


def _fingerprint(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _words(item: Conversation) -> str:
    parts = [
        block.text
        for message in item.messages
        for block in message.content
        if block.type in ("text", "thinking")
    ]
    return " ".join(" ".join(parts).split())


def _one_conversation_bundle(source: Bundle, item: Conversation, where: Path) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    made = Bundle.create(
        where,
        Manifest(
            created_at=datetime.now(UTC),
            created_by=f"ferry v{__version__} (self-check)",
            source_machine=SourceMachine(
                hostname=platform.node() or None,
                os=sys.platform,  # type: ignore[arg-type]
                user_home=str(Path.home()),
            ),
        ),
        force=True,
    )
    for path in source.conversation_files(item.id):
        target = where / path.relative_to(source.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    made.manifest.conversation_count = 1
    made.manifest.tools_included = [item.source_tool]
    made._write_manifest()
    return where


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------


def check_export(state: State) -> Result:
    """A real bundle, from every tool installed here."""
    found: list[str] = []
    for adapter in _adapters():
        if adapter.detect().installed:
            list(adapter.export(state.root))
            found.append(adapter.display_name)
    if not found:
        return Result(None, "no assistant with a store on this machine")
    state.bundle = Bundle.open(state.root)
    state.fingerprint_before = _fingerprint(state.root)
    state.conversations = [
        state.bundle.load_conversation(c) for c in state.bundle.list_conversations()
    ]
    for item in state.conversations:
        current = state.smallest.get(item.source_tool)
        if current is None or len(item.messages) < len(current.messages):
            state.smallest[item.source_tool] = item
    return Result(True, f"{len(state.conversations)} conversations from {', '.join(found)}")


def check_the_table_is_complete(state: State) -> Result:
    """Every pair has an answer, and every refusal has a reason."""
    missing: list[str] = []
    for source in TOOLS:
        for target in TOOLS:
            found = pair(source, target)
            if source == target:
                if found.support != "native":
                    missing.append(f"{source}->{target}")
                continue
            if found.support == "unsupported" and len(found.reason) < 40:
                missing.append(f"{source}->{target} has no usable reason")
    if missing:
        return Result(False, "; ".join(missing[:3]))
    # Asked from a source that is not the target, or the answer comes back
    # "native" and a supported target counts as zero -- which is how this line
    # read the first time it ran.
    supported = sorted(
        {
            target
            for source in TOOLS
            for target in TOOLS
            if source != target and pair(source, target).support == "supported"
        }
    )
    return Result(
        True,
        f"{len(TOOLS) ** 2} pairs answered, supported into: {', '.join(supported) or 'nothing'}",
    )


def check_a_pair_really_migrates(state: State) -> Result:
    """The scope guard: at least one ordered pair, end to end.

    Imported into a scratch store and then exported *back out*, with the words
    compared. This is the check the M7b probe was written to make, and the one
    that caught the importer reporting a success it had not achieved.
    """
    if not state.bundle:
        return Result(None, "nothing exported")
    worked: list[str] = []
    for source, item in sorted(state.smallest.items()):
        if source == "claude-code":
            continue
        pair_dir = state.workspace / f"{source}-to-claude-code"
        held = _one_conversation_bundle(state.bundle, item, pair_dir / "in")
        adapter = _build("claude-code", _scratch("claude-code", pair_dir / "store"))
        wrote = sum(
            1
            for event in adapter.import_(held, ImportOptions(allow_cross_tool=True, backup=False))
            if event.kind == "progress"
        )
        if not wrote:
            return Result(False, f"{source} -> claude-code wrote nothing")
        back = pair_dir / "out"
        list(adapter.export(back))
        returned = Bundle.open(back)
        ids = returned.list_conversations()
        if not ids:
            return Result(False, f"{source} -> claude-code wrote, but nothing came back")
        came = returned.load_conversation(ids[0])
        before, after = _words(item), _words(came)
        if len(came.messages) != len(item.messages) or after != before:
            return Result(
                False,
                f"{source} -> claude-code came back with "
                f"{len(came.messages)}/{len(item.messages)} messages",
            )
        worked.append(source)
    if not worked:
        return Result(None, "only one tool has conversations on this machine")
    return Result(True, f"{len(worked)} pairs round-tripped whole: {', '.join(worked)}")


def check_a_foreign_file_is_not_installed(state: State) -> Result:
    """The false write, kept fixed.

    A Claude Code conversation carries a ``source_raw`` sidecar, and so does an
    Antigravity one. Only one of them is a database. The importer used to ask
    whether the file was *there*.
    """
    if not state.bundle or "claude-code" not in state.smallest:
        return Result(None, "no Claude Code conversation to offer")
    item = state.smallest["claude-code"]
    pair_dir = state.workspace / "false-write"
    held = _one_conversation_bundle(state.bundle, item, pair_dir / "in")
    store = pair_dir / "store"
    adapter = AntigravityAdapter(_scratch("antigravity", store))

    wrote = sum(
        1
        for event in adapter.import_(held, ImportOptions(allow_cross_tool=True, backup=False))
        if event.kind == "progress"
    )
    landed = [p for p in store.rglob("*.db") if p.is_file()]
    if wrote or landed:
        return Result(False, f"{wrote} reported written, {len(landed)} .db files created")
    return Result(True, "reported nothing written, and wrote nothing")


def check_every_refusal_says_why(state: State) -> Result:
    """A refusal with no reason leaves someone with nowhere to go."""
    if not state.bundle:
        return Result(None, "nothing exported")
    checked = 0
    for target in ("codex", "copilot", "antigravity"):
        for source, item in state.smallest.items():
            if source == target:
                continue
            pair_dir = state.workspace / f"refuse-{source}-{target}"
            held = _one_conversation_bundle(state.bundle, item, pair_dir / "in")
            adapter = _build(target, _scratch(target, pair_dir / "store"))
            events = list(adapter.import_(held, ImportOptions(allow_cross_tool=True, backup=False)))
            if any(e.kind == "progress" for e in events):
                return Result(False, f"{source} -> {target} wrote something")
            said = [e.message for e in events if e.kind in ("skipped", "error")]
            if not said or len(said[0]) < 40:
                return Result(False, f"{source} -> {target} refused without a reason")
            checked += 1
    return Result(True, f"{checked} refusals, all with a reason and no partial write")


def check_nothing_is_half_written(state: State) -> Result:
    """A refused import must leave the target exactly as it found it."""
    if not state.bundle or not state.smallest:
        return Result(None, "nothing exported")
    empty = 0
    for target in ("codex", "copilot", "antigravity"):
        pair_dir = state.workspace / f"clean-{target}"
        store = pair_dir / "store"
        source, item = next(iter(sorted(state.smallest.items())))
        if source == target:
            continue
        held = _one_conversation_bundle(state.bundle, item, pair_dir / "in")
        adapter = _build(target, _scratch(target, store))
        list(adapter.import_(held, ImportOptions(allow_cross_tool=True, backup=False)))
        if any(p.is_file() for p in store.rglob("*")):
            return Result(False, f"{target} left files behind after refusing")
        empty += 1
    return Result(True, f"{empty} refused targets left with nothing in them")


def check_the_cost_is_counted(state: State) -> Result:
    """What a conversion loses is counted from the conversation, not recited."""
    if not state.conversations:
        return Result(None, "nothing exported")
    total = 0
    for item in state.conversations:
        loss = assess(item, "claude-code")
        if item.source_tool == "claude-code":
            if loss.notes:
                return Result(False, "a native import was described as lossy")
            continue
        if CEILING not in loss.notes:
            return Result(False, f"{item.source_tool} conversion did not state the ceiling")
        if not any("stays attributed to" in note for note in loss.notes):
            return Result(False, f"{item.source_tool} conversion did not keep attribution")
        total += loss.degraded
    return Result(True, f"{total} blocks would be degraded across this machine's history")


def check_nothing_moved(state: State) -> Result:
    """The mandatory one: the bundle everything read is untouched."""
    if not state.fingerprint_before:
        return Result(None, "nothing exported")
    after = _fingerprint(state.root)
    if after != state.fingerprint_before:
        changed = set(after) ^ set(state.fingerprint_before)
        return Result(False, f"{len(changed) or 'some'} files differ")
    return Result(True, f"all {len(after)} checksums match")


CHECKS = [
    ("export a real bundle", check_export),
    ("every pair has an answer, every refusal a reason", check_the_table_is_complete),
    ("a supported pair migrates and reads back whole", check_a_pair_really_migrates),
    ("a foreign file is never installed as a database", check_a_foreign_file_is_not_installed),
    ("an unsupported pair is refused, with a reason", check_every_refusal_says_why),
    ("a refused import leaves nothing behind", check_nothing_is_half_written),
    ("what a conversion costs is counted", check_the_cost_is_counted),
    ("the bundle it read is untouched", check_nothing_moved),
]


def main() -> int:
    state = State()
    print("Ferry self-check - M7b (cross-tool migration)")
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
