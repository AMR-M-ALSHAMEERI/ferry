"""M1 self-check: bundle create -> write -> zip -> unzip -> read -> compare.

Layer 2 verification per PLAN.md §6.8.2. Maintained code, not a throwaway spike.

M1 has no adapters yet, so there is no real conversation data to read — this runs
entirely on synthetic objects in a temp directory. It never touches any AI tool's
storage, and never prints conversation content.

Run:  python scripts/selfcheck/verify_m1.py
Exits non-zero if any check fails.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from ferry.core import Bundle, BundleError, Manifest, SourceMachine, sha256_file
from ferry.ucs import (
    UCS_VERSION,
    Attachment,
    Conversation,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Workspace,
)

results: list[tuple[str, bool | None, str]] = []


def record(name: str, ok: bool | None, detail: str = "") -> None:
    results.append((name, ok, detail))


def build_conversation(conversation_id: UUID) -> Conversation:
    return Conversation(
        id=conversation_id,
        source_tool="claude-code",
        source_tool_version="selfcheck",
        source_id="selfcheck-session",
        title="M1 self-check conversation",
        created_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 21, 9, 30, tzinfo=UTC),
        workspace=Workspace(name="proj", original_path="/tmp/proj", path_hash="-tmp-proj"),
        messages=[
            Message(
                role="user",
                content=[TextBlock(text="synthetic user turn")],
                timestamp=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
            ),
            Message(
                role="assistant",
                content=[
                    ThinkingBlock(text="synthetic reasoning", signature="sig"),
                    TextBlock(text="synthetic reply"),
                    ToolUseBlock(name="Bash", input={"command": "echo hi"}),
                ],
                timestamp=datetime(2026, 8, 21, 9, 1, tzinfo=UTC),
                model="synthetic-model",
            ),
            Message(
                role="tool",
                content=[ToolResultBlock(tool_use_id="t1", output="hi")],
                timestamp=None,
            ),
        ],
    )


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="ferry-selfcheck-m1-"))
    try:
        run_checks(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print(f"Ferry self-check — M1 (UCS v{UCS_VERSION} + bundle format)")
    passed = failed = skipped = 0
    for index, (name, ok, detail) in enumerate(results, start=1):
        if ok is None:
            status, skipped = "SKIP", skipped + 1
        elif ok:
            status, passed = "PASS", passed + 1
        else:
            status, failed = "FAIL", failed + 1
        dots = "." * max(3, 44 - len(name))
        suffix = f" ({detail})" if detail else ""
        print(f"  {index}. {name} {dots} {status}{suffix}")

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


def run_checks(workdir: Path) -> None:
    conversation_id = uuid4()
    conversation = build_conversation(conversation_id)
    build_root = workdir / "build"

    # 1. create
    try:
        manifest = Manifest(
            created_at=datetime.now(UTC),
            created_by="ferry selfcheck",
            source_machine=SourceMachine(hostname=None, os="linux", user_home=None),
        )
        bundle = Bundle.create(build_root, manifest)
        record("Bundle created with manifest", (build_root / "manifest.json").is_file())
    except Exception as exc:
        record("Bundle created with manifest", False, type(exc).__name__)
        return

    # 2. add conversation
    try:
        bundle.add_conversation(conversation)
        ok = bundle.list_conversations() == [conversation_id]
        record("Conversation written to bundle", ok, "1 conversation")
    except Exception as exc:
        record("Conversation written to bundle", False, type(exc).__name__)
        return

    # 3. attachment written and checksum-verified
    try:
        src = workdir / "asset.bin"
        src.write_bytes(b"synthetic-attachment-bytes" * 16)
        attachment = Attachment(
            id=uuid4(),
            filename="asset.bin",
            mime_type="application/octet-stream",
            bundle_path="",
            sha256=sha256_file(src),
        )
        attachment.bundle_path = f"attachments/{conversation_id}/{attachment.id}.bin"
        conversation.attachments = [attachment]
        bundle.add_conversation(conversation)
        dest = bundle.add_attachment(conversation_id, src, attachment)
        record("Attachment copied and checksum verified", dest.is_file())
    except Exception as exc:
        record("Attachment copied and checksum verified", False, type(exc).__name__)

    # 4. validate clean
    problems = bundle.validate()
    record("Bundle validates clean", problems == [], f"{len(problems)} problems")

    # 5. pack
    archive = workdir / "bundle.zip"
    try:
        bundle.pack(archive)
        record(
            "Bundle packs to zip", zipfile.is_zipfile(archive), f"{archive.stat().st_size} bytes"
        )
    except Exception as exc:
        record("Bundle packs to zip", False, type(exc).__name__)
        return

    # 6. overwrite refused without force
    try:
        bundle.pack(archive)
        record("Refuses to overwrite without force", False, "overwrite was allowed")
    except BundleError:
        record("Refuses to overwrite without force", True)

    # 7. unpack
    try:
        reopened = Bundle.unpack(archive, workdir / "extracted")
        record("Bundle unpacks and reopens", reopened.list_conversations() == [conversation_id])
    except Exception as exc:
        record("Bundle unpacks and reopens", False, type(exc).__name__)
        return

    # 8. round-trip fidelity, compared as raw JSON
    try:
        restored = reopened.load_conversation(conversation_id)
        identical = json.loads(restored.model_dump_json()) == json.loads(
            conversation.model_dump_json()
        )
        record("Round-trip is byte-identical", identical)
    except Exception as exc:
        record("Round-trip is byte-identical", False, type(exc).__name__)

    # 9. extracted bundle validates too
    record("Extracted bundle validates clean", reopened.validate() == [])

    # 10. path traversal refused
    evil = workdir / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escaped.txt", "should never be written")
    try:
        Bundle.unpack(evil, workdir / "evil-dest")
        record("Rejects zip path traversal", False, "traversal was allowed")
    except BundleError:
        record("Rejects zip path traversal", not (workdir / "escaped.txt").exists())

    # 11. corruption is detected rather than trusted
    try:
        target = reopened._resolve_inside(conversation.attachments[0].bundle_path)
        target.write_bytes(b"corrupted")
        record("Detects corrupted attachment", reopened.validate() != [])
    except Exception as exc:
        record("Detects corrupted attachment", False, type(exc).__name__)


if __name__ == "__main__":
    sys.exit(main())
