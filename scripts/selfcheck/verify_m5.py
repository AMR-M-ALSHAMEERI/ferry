"""Layer-2 self-check for M5 -- the Copilot Chat adapter.

Contract (PLAN.md 6.8.2): numbered PASS/FAIL/SKIP lines, non-zero exit on any
failure, runs against **real data on this machine**, never prints conversation
content, and never modifies user data.

Check 11 is the mandatory one: every chat transcript is checksummed before and
after, asserting export did not move a byte of the real store.

Three checks exist only for Copilot:

- **Check 4** replays every real file and asserts nothing was skipped. The
  format is a delta log, and a reader that quietly drops records returns a
  shorter conversation with no error anywhere.
- **Check 5** re-derives the ``workspaceStorage`` key for every workspace on
  this machine and compares it with the real directory name. That derivation
  was M5's blocking unknown, it depends on a filesystem timestamp, and it
  differs by platform -- so it is verified against the disk rather than trusted.
- **Check 10** asserts the imported conversations are **listed** in
  ``chat.ChatSessionStore.index``. A transcript VS Code does not list is a
  conversation the user can never reach, and it looks exactly like success.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from ferry.adapters.base import ImportOptions
from ferry.adapters.copilot import CopilotAdapter
from ferry.adapters.copilot import paths as cp
from ferry.adapters.copilot.deltas import replay_lines
from ferry.adapters.copilot.reader import read_session
from ferry.adapters.copilot.writer import index_lists
from ferry.core import Bundle, sha256_file
from ferry.ucs import UCS_VERSION, Conversation


@dataclass
class Result:
    ok: bool | None
    detail: str


class State:
    def __init__(self) -> None:
        self.adapter = CopilotAdapter()
        self.workspace = Path(tempfile.mkdtemp(prefix="ferry-verify-m5-"))
        self.bundle_dir = self.workspace / "bundle"
        self.target_user = self.workspace / "target" / "Code" / "User"
        self.target_env = dict(os.environ)
        self.target_env[cp.USER_DIR_ENV] = str(self.target_user)
        self.source_before: dict[str, str] = {}
        self.detected = self.adapter.detect()
        self.exported = 0
        self.imported = 0

    def source_files(self) -> list[Path]:
        user = cp.user_dir()
        if not user.is_dir():
            return []
        found = [path for _, path in cp.session_files()]
        images = cp.chat_images_dir()
        if images.is_dir():
            found.extend(p for p in sorted(images.rglob("*")) if p.is_file())
        return sorted(found)

    def fingerprint(self) -> dict[str, str]:
        return {str(p): sha256_file(p) for p in self.source_files()}

    def cleanup(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)


def check_installed(state: State) -> Result:
    if not state.detected.installed:
        return Result(None, "; ".join(state.detected.notes) or "not installed")
    return Result(True, f"{state.detected.conversation_count_estimate} chat sessions")


def check_data_directory(state: State) -> Result:
    if not state.detected.installed:
        return Result(None, "no data to find")
    sessions = cp.session_files()
    workspaces = {key for key, _ in sessions if key}
    total = sum(path.stat().st_size for _, path in sessions)
    return Result(
        True,
        f"{len(sessions)} sessions, {total / 1024:.0f} KB, "
        f"{len(workspaces)} workspaces, {sum(1 for k, _ in sessions if not k)} no-folder",
    )


def check_source_fingerprinted(state: State) -> Result:
    if not state.detected.installed:
        return Result(None, "nothing to fingerprint")
    state.source_before = state.fingerprint()
    total = sum(Path(p).stat().st_size for p in state.source_before)
    return Result(True, f"{len(state.source_before)} files, {total:,} bytes")


def check_every_delta_replays(state: State) -> Result:
    """Nothing skipped across every real transcript.

    A skipped record is not an error to the replayer -- by design, so one bad
    line does not cost a conversation. That makes this the only place a
    creeping format change shows up before a user notices missing messages.
    """
    if not state.detected.installed:
        return Result(None, "nothing to replay")
    records = applied = skipped = 0
    unknown: dict[int, int] = {}
    for _, path in cp.session_files():
        replayed = replay_lines(path.read_text(encoding="utf-8", errors="replace").splitlines())
        applied += replayed.applied
        skipped += replayed.skipped
        records += replayed.applied + replayed.skipped
        for kind, count in replayed.unknown_kinds.items():
            unknown[kind] = unknown.get(kind, 0) + count
    if skipped:
        return Result(False, f"{skipped} of {records} records skipped, unknown kinds {unknown}")
    return Result(True, f"{applied} records, all applied")


def check_workspace_keys_reproduce(state: State) -> Result:
    """Ferry derives the same directory names VS Code did.

    Verified against the disk because the rule was read from VS Code's source
    and depends on a filesystem timestamp that differs by platform. A wrong
    derivation does not raise -- it names a directory that does not exist.
    """
    root = cp.workspace_storage()
    if not root.is_dir():
        return Result(None, "no workspace storage")

    matched = 0
    unexplained: list[str] = []
    for entry in sorted(root.iterdir()):
        marker = entry / "workspace.json"
        if not marker.is_file():
            continue  # empty-window ids are random by design
        try:
            meta = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        uri = meta.get("folder") or meta.get("workspace")
        if not isinstance(uri, str):
            continue
        target = Path(urllib.parse.unquote(uri.removeprefix("file:///")))
        derived = cp.folder_key(target) if "folder" in meta else cp.workspace_file_key(target)
        if derived == entry.name:
            matched += 1
        else:
            unexplained.append(entry.name[:8])

    if matched == 0:
        return Result(None, "no derivable workspaces on this machine")
    detail = f"{matched} reproduced"
    if unexplained:
        # One 8-hex directory here has never matched and holds no chats. It is
        # reported, not treated as a failure, because it cannot lose anything.
        holds_chats = [k for k in unexplained if (root / k).is_dir()]
        detail += f", {len(unexplained)} not derivable ({', '.join(unexplained)})"
        if any((root / k / "chatSessions").is_dir() for k in holds_chats):
            return Result(False, detail + " - one of them holds conversations")
    return Result(True, detail)


def check_export(state: State) -> Result:
    if not state.detected.installed:
        return Result(None, "nothing to export")
    errors = 0
    for event in state.adapter.export(state.bundle_dir):
        if event.kind == "progress":
            state.exported += 1
        elif event.kind == "error":
            errors += 1
    if errors:
        return Result(False, f"{errors} errors during export")
    if not state.exported:
        return Result(None, "no conversations had any messages")
    return Result(True, f"{state.exported} conversations exported")


def check_bundle_validates(state: State) -> Result:
    if not state.exported:
        return Result(None, "nothing exported")
    problems = Bundle.open(state.bundle_dir).validate()
    if problems:
        return Result(False, f"{len(problems)} problems: {problems[0]}")
    return Result(True, "manifest, conversations and attachments all consistent")


def check_schema_conformance(state: State) -> Result:
    if not state.exported:
        return Result(None, "nothing exported")
    bundle = Bundle.open(state.bundle_dir)
    messages = attachments = 0
    for conversation_id in bundle.list_conversations():
        conversation = bundle.load_conversation(conversation_id)
        if conversation.ucs_version != UCS_VERSION:
            return Result(False, f"{conversation_id} is UCS {conversation.ucs_version}")
        Conversation.model_validate(conversation.model_dump())
        messages += len(conversation.messages)
        attachments += len(conversation.attachments)
    return Result(True, f"UCS {UCS_VERSION}, {messages} messages, {attachments} attachments")


def check_import_to_temp(state: State) -> Result:
    """Import into a scratch VS Code user directory, never the real one."""
    if not state.exported:
        return Result(None, "nothing to import")
    state.target_user.mkdir(parents=True, exist_ok=True)
    errors = []
    for event in CopilotAdapter(state.target_env).import_(state.bundle_dir, ImportOptions()):
        if event.kind == "progress":
            state.imported += 1
        elif event.kind == "error":
            errors.append(event.message)
    if errors:
        return Result(False, f"{len(errors)} errors: {errors[0][:70]}")
    return Result(True, f"{state.imported} conversations written to a scratch store")


def check_round_trip(state: State) -> Result:
    """Read back what was written and compare it block for block.

    This is the check that catches what no unit test does. On Codex it found
    four bugs in an afternoon, and here it found the export producing a
    different bundle every time it ran.
    """
    if not state.imported:
        return Result(None, "nothing imported")
    bundle = Bundle.open(state.bundle_dir)
    written = cp.session_files(state.target_env)
    compared = 0
    for key, path in written:
        rebuilt = read_session(path, key, state.target_env).conversation
        if rebuilt is None:
            return Result(False, f"{path.stem[:8]} read back with no messages")
        original = bundle.load_conversation(rebuilt.id)
        if rebuilt.title != original.title:
            return Result(False, f"{path.stem[:8]} title changed")
        if len(rebuilt.messages) != len(original.messages):
            return Result(
                False,
                f"{path.stem[:8]} {len(original.messages)} messages became {len(rebuilt.messages)}",
            )
        for left, right in zip(original.messages, rebuilt.messages, strict=True):
            if [b.model_dump() for b in left.content] != [b.model_dump() for b in right.content]:
                return Result(False, f"{path.stem[:8]} content differs")
        compared += 1
    return Result(True, f"{compared} conversations identical after a full round trip")


def check_conversations_are_listed(state: State) -> Result:
    """VS Code shows what the index lists, not what is on disk.

    A transcript written without its entry is a conversation the user can never
    reach and has no way to discover exists -- and the import that produced it
    looks entirely successful.
    """
    if not state.imported:
        return Result(None, "nothing imported")
    written = cp.session_files(state.target_env)
    missing: list[str] = []
    for key, path in written:
        database = (
            cp.workspace_storage(state.target_env) / key / "state.vscdb"
            if key
            else cp.global_storage(state.target_env) / "state.vscdb"
        )
        if path.stem not in index_lists(database):
            missing.append(path.stem[:8])
    if missing:
        return Result(False, f"{len(missing)} transcripts are invisible: {', '.join(missing)}")
    return Result(True, f"all {len(written)} listed in chat.ChatSessionStore.index")


def check_source_unmodified(state: State) -> Result:
    """The mandatory one. Export and import must not move a byte of real data."""
    if not state.source_before:
        return Result(None, "nothing was fingerprinted")
    after = state.fingerprint()
    if after != state.source_before:
        changed = sorted(
            set(after) ^ set(state.source_before)
            | {
                k
                for k in after.keys() & state.source_before.keys()
                if after[k] != state.source_before[k]
            }
        )
        return Result(False, f"{len(changed)} files changed: {Path(changed[0]).name}")
    return Result(True, f"{len(after)} files, all checksums match")


def main() -> int:
    state = State()
    checks = [
        ("Copilot Chat installed", check_installed),
        ("Chat stores found", check_data_directory),
        ("Source fingerprinted", check_source_fingerprinted),
        ("Every delta replays", check_every_delta_replays),
        ("Workspace keys reproduce", check_workspace_keys_reproduce),
        ("Export", check_export),
        ("Bundle validates", check_bundle_validates),
        ("UCS schema conformance", check_schema_conformance),
        ("Import to temp dir", check_import_to_temp),
        ("Conversations are listed", check_conversations_are_listed),
        ("Source data unmodified", check_source_unmodified),
    ]
    # Round-trip runs before the listing check reads the same store; order in
    # the printed list follows the contract's reading order instead.
    checks.insert(9, ("Round-trip fidelity", check_round_trip))

    print("Ferry self-check - M5 (Copilot Chat adapter)")
    passed = failed = skipped = 0
    try:
        for number, (label, run) in enumerate(checks, start=1):
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
            dots = "." * max(3, 44 - len(label))
            print(f"  {number}. {label} {dots} {status} ({result.detail})")
    finally:
        state.cleanup()

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
