"""The checked-in JSON schema must match the models it was generated from.

Without this, schemas/ucs-1.2.json silently rots the first time someone edits
ferry.ucs.models and forgets to rerun scripts/gen_schema.py.
"""

import json
import sys
from pathlib import Path

from ferry.ucs import UCS_VERSION

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from gen_schema import build_schema, schema_path  # noqa: E402


def test_schema_file_exists_for_current_version() -> None:
    assert schema_path().name == f"ucs-{UCS_VERSION}.json"
    assert schema_path().is_file(), "run: python scripts/gen_schema.py"


def test_checked_in_schema_matches_models() -> None:
    on_disk = json.loads(schema_path().read_text(encoding="utf-8"))
    assert on_disk == build_schema(), (
        "schemas/ucs-*.json is out of date with ferry.ucs.models — "
        "rerun: python scripts/gen_schema.py"
    )


def test_schema_documents_every_conversation_field() -> None:
    on_disk = json.loads(schema_path().read_text(encoding="utf-8"))
    expected = {
        "ucs_version",
        "id",
        "source_tool",
        "source_tool_version",
        "source_id",
        "title",
        "created_at",
        "updated_at",
        "workspace",
        "messages",
        "attachments",
        "provenance",
        "source_raw",
    }
    assert set(on_disk["properties"]) == expected


def test_schema_defines_all_four_content_blocks() -> None:
    on_disk = json.loads(schema_path().read_text(encoding="utf-8"))
    defs = set(on_disk["$defs"])
    assert {"TextBlock", "ThinkingBlock", "ToolUseBlock", "ToolResultBlock"} <= defs
