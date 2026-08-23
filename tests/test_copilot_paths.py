"""Locating VS Code's chat stores, and naming its workspace directories.

The key derivation is the part worth testing hardest. It was blocking for M5
for a reason: the digest covers the folder's **creation time** as well as its
path, so it cannot be guessed, and getting it wrong does not raise -- it names
a directory that does not exist, and an import would write where VS Code never
looks.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from ferry.adapters.copilot import paths as cp


@pytest.fixture
def user_dir(tmp_path: Path, monkeypatch) -> Path:  # type: ignore[no-untyped-def]
    root = tmp_path / "Code" / "User"
    root.mkdir(parents=True)
    monkeypatch.setenv(cp.USER_DIR_ENV, str(root))
    return root


def test_the_override_wins_over_the_platform_default(user_dir: Path) -> None:
    assert cp.user_dir() == user_dir


def test_both_stores_are_searched(user_dir: Path) -> None:
    """A conversation is in one store or the other depending on whether a
    folder was open. Reading only one silently halves the export."""
    workspace = user_dir / "workspaceStorage" / "deadbeef" / "chatSessions"
    workspace.mkdir(parents=True)
    (workspace / "11111111-1111-4111-8111-111111111111.jsonl").write_text("{}", encoding="utf-8")
    empty = user_dir / "globalStorage" / cp.EMPTY_WINDOW_DIR
    empty.mkdir(parents=True)
    (empty / "22222222-2222-4222-8222-222222222222.jsonl").write_text("{}", encoding="utf-8")

    found = cp.session_files()

    assert [key for key, _ in found] == ["deadbeef", ""]


def test_a_workspace_with_no_chats_is_not_listed(user_dir: Path) -> None:
    (user_dir / "workspaceStorage" / "quiet").mkdir(parents=True)

    assert cp.session_files() == []


def test_missing_stores_are_not_an_error(user_dir: Path) -> None:
    assert cp.session_files() == []


def test_the_image_store_sits_beside_the_workspaces_not_inside_one(user_dir: Path) -> None:
    """It is shared across every workspace, so an import must not assume it
    belongs to the conversation that brought it."""
    assert cp.chat_images_dir().parent == cp.workspace_storage()


def test_a_session_id_comes_from_the_filename() -> None:
    path = Path("11111111-1111-4111-8111-111111111111.jsonl")

    assert str(cp.session_id_of(path)) == "11111111-1111-4111-8111-111111111111"


def test_a_filename_that_is_not_an_id_is_refused() -> None:
    assert cp.session_id_of(Path("notes.jsonl")) is None


# --------------------------------------------------------------------------
# key derivation
# --------------------------------------------------------------------------


def test_a_folder_key_matches_what_vs_code_would_compute(tmp_path: Path) -> None:
    """Reproduces VS Code's own rule: md5 over the path *and* the folder's
    identity on disk -- inode on Linux, birth time everywhere else."""
    folder = tmp_path / "project"
    folder.mkdir()
    stat = folder.stat()
    if sys.platform == "linux":
        stamp = str(stat.st_ino)
    else:
        # st_birthtime is absent on Windows before 3.12, where st_ctime is the
        # creation time. The test has to span the same versions Ferry does.
        birth = getattr(stat, "st_birthtime", None)
        stamp = str(int((birth if birth is not None else stat.st_ctime) * 1000))
    expected = hashlib.md5((cp.vscode_fs_path(folder) + stamp).encode()).hexdigest()  # noqa: S324

    assert cp.folder_key(folder) == expected


def test_a_folder_key_is_produced_on_every_python_ferry_supports(tmp_path: Path) -> None:
    """The regression CI caught: `st_birthtime` does not exist on Windows
    before 3.12, and without a fallback the derivation returned None on the
    oldest Python Ferry claims to run on -- with no error anywhere."""
    folder = tmp_path / "project"
    folder.mkdir()

    assert cp.folder_key(folder) is not None


def test_a_folder_key_is_stable_across_calls(tmp_path: Path) -> None:
    folder = tmp_path / "project"
    folder.mkdir()

    assert cp.folder_key(folder) == cp.folder_key(folder)


def test_two_folders_with_the_same_name_get_different_keys(tmp_path: Path) -> None:
    """The whole point of mixing in the timestamp: identical paths under
    different roots, or a folder recreated at the same path, are not the same
    workspace to VS Code."""
    a = tmp_path / "one" / "project"
    b = tmp_path / "two" / "project"
    a.mkdir(parents=True)
    b.mkdir(parents=True)

    assert cp.folder_key(a) != cp.folder_key(b)


def test_a_folder_that_is_not_there_yields_no_key(tmp_path: Path) -> None:
    """VS Code declines to produce an id when it cannot stat the folder, and
    inventing one here would name a directory nothing will ever read."""
    assert cp.folder_key(tmp_path / "gone") is None


def test_a_workspace_file_key_uses_the_path_alone(tmp_path: Path) -> None:
    """No timestamp for a `.code-workspace` file -- and it is lowercased
    everywhere except Linux, where paths are case-sensitive."""
    workspace = tmp_path / "Team.code-workspace"
    text = cp.vscode_fs_path(workspace)
    if sys.platform != "linux":
        text = text.lower()
    expected = hashlib.md5(text.encode()).hexdigest()  # noqa: S324

    assert cp.workspace_file_key(workspace) == expected


def test_a_workspace_file_key_needs_no_file_on_disk(tmp_path: Path) -> None:
    """Unlike a folder: nothing is stat'd, so an import can compute the key for
    a workspace file before it exists."""
    assert cp.workspace_file_key(tmp_path / "absent.code-workspace")


@pytest.mark.skipif(sys.platform != "win32", reason="drive letters are a Windows spelling")
def test_a_windows_drive_letter_is_lowercased() -> None:
    """VS Code normalises the drive when it parses the URI and hashes that
    spelling. Feeding it `C:` where VS Code used `c:` produces a different,
    entirely plausible-looking directory name."""
    assert cp.vscode_fs_path(Path("C:/Users/x")).startswith("c:")
