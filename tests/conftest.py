"""Shared fixtures. Synthetic UCS objects only — no real conversation data."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from ferry.core import Manifest, SourceMachine
from ferry.core import provenance as provenance_store
from ferry.core.backup import BACKUP_ENV
from ferry.core.sealed import OPEN_ENV
from ferry.skill import SKILLS_HOME_ENV
from ferry.ucs import (
    Attachment,
    Conversation,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    Workspace,
)


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path_factory, monkeypatch):
    """Point Ferry's settings file at a temp location for every test.

    ``resolve_theme`` consults ``~/.ferry/config.json``, so without this a
    developer who has saved a theme would get different results from CI. Tests
    must never read or write the real user config.
    """
    fake = tmp_path_factory.mktemp("ferry-config") / "config.json"
    monkeypatch.setattr("ferry.config.CONFIG_PATH", fake)
    # Ferry's provenance store lives under the same `~/.ferry`, and an import
    # writes into it. Without this the suite left records in the developer's
    # own home -- thirteen of them, the first time the feature ran under it.
    monkeypatch.setenv(provenance_store.ROOT_ENV, str(fake.parent / "provenance"))
    # And its backups. Deleting takes a copy of everything it removes, so a
    # suite that deletes would otherwise fill the developer's real backups
    # folder with copies of test fixtures.
    monkeypatch.setenv(BACKUP_ENV, str(fake.parent / "backups"))
    # And where a sealed bundle is opened. Without this every sealed-bundle
    # test unsealed into the developer's real ~/.ferry/open, and the real
    # backups folder filled with copies whose originals were under it.
    monkeypatch.setenv(OPEN_ENV, str(fake.parent / "open"))
    # And where Codex and Copilot keep a person's skills, which is under the
    # home folder itself: `ferry skill --install` would otherwise write into the
    # developer's real ~/.agents and ~/.copilot.
    monkeypatch.setenv(SKILLS_HOME_ENV, str(fake.parent / "skills-home"))
    return fake


ASSISTANT_HOME_VARS = (
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "FERRY_VSCODE_USER_DIR",
    "FERRY_ANTIGRAVITY_DIR",
    "FERRY_ANTIGRAVITY_INSTALL",
)
"""Every environment variable that points an adapter at a real conversation store.

**Add to this the moment an adapter learns to read a new one.** It has already
been forgotten once per adapter: the omission is invisible in CI, where no tool
is installed, and only shows up as a failure on a machine that actually uses
the thing.
"""


@pytest.fixture(autouse=True)
def _isolate_assistant_data(tmp_path_factory, monkeypatch):
    """Point every adapter at an empty conversation store.

    Without this the suite reads the developer's own history. A test asserting
    "nothing is installed" then passes in CI and fails locally, and — far worse
    — any test that ever grew a write would be writing into real conversations.

    Adapters constructed with an explicit ``env=`` are unaffected, which is how
    the adapters' own tests lay out fixtures.
    """
    empty = tmp_path_factory.mktemp("assistant-data")
    for variable in ASSISTANT_HOME_VARS:
        monkeypatch.setenv(variable, str(empty / variable.lower()))
    return empty


CONV_ID = UUID("11111111-1111-4111-8111-111111111111")
ATTACH_ID = UUID("22222222-2222-4222-8222-222222222222")


@pytest.fixture
def manifest() -> Manifest:
    return Manifest(
        created_at=datetime(2026, 8, 21, 10, 0, 0, tzinfo=UTC),
        created_by="ferry v0.1.0",
        source_machine=SourceMachine(hostname="testhost", os="win32", user_home="C:/Users/test"),
    )


@pytest.fixture
def conversation() -> Conversation:
    """A conversation exercising all four content block types."""
    return Conversation(
        id=CONV_ID,
        source_tool="claude-code",
        source_tool_version="2.1.234",
        source_id="session-abc",
        title="Synthetic test conversation",
        created_at=datetime(2026, 8, 20, 9, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 20, 9, 30, 0, tzinfo=UTC),
        workspace=Workspace(
            name="proj",
            original_path="C:/Users/test/proj",
            path_hash="C--Users-test-proj",
        ),
        messages=[
            Message(
                role="user",
                content=[TextBlock(text="first user turn")],
                timestamp=datetime(2026, 8, 20, 9, 0, 0, tzinfo=UTC),
            ),
            Message(
                role="assistant",
                content=[
                    ThinkingBlock(text="reasoning text", signature="sig-abc"),
                    TextBlock(text="assistant reply"),
                    ToolUseBlock(name="Bash", input={"command": "ls"}),
                ],
                timestamp=datetime(2026, 8, 20, 9, 1, 0, tzinfo=UTC),
                model="claude-opus-5",
            ),
            Message(
                role="tool",
                content=[ToolResultBlock(tool_use_id="tool-1", output="file-a\nfile-b")],
                timestamp=None,
            ),
        ],
    )


@pytest.fixture
def attachment_record() -> Attachment:
    """sha256 is filled in by the test once the file exists — this is the shape only."""
    return Attachment(
        id=ATTACH_ID,
        filename="shot.png",
        mime_type="image/png",
        bundle_path=f"attachments/{CONV_ID}/{ATTACH_ID}.png",
        sha256="",
    )
