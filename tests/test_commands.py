"""``ferry export`` and ``ferry import``, run the way a script runs them.

Through the real command line with no terminal, against Claude Code stores laid
out in temporary folders from the anonymised fixtures. The adapters are the
real ones; what is under test is that every question the screens ask has a flag
here, that a missing answer is an error rather than a hang, and that the safe
answer is still the one taken when nothing is said.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import typer.main
from typer.testing import CliRunner

from ferry.adapters import REGISTRY
from ferry.adapters.claude_code.paths import mangle
from ferry.cli import app
from ferry.cli.brand import TAGLINE
from ferry.cli.commands import FAILED, OK, REFUSED, export_bundle, import_bundle
from ferry.cli.theme import THEMES, Capability
from ferry.cli.ui import UI
from ferry.core import Bundle, Manifest, provenance
from ferry.core.backup import backup_root
from ferry.core.sealed import is_sealed
from ferry.ucs import Conversation

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code"
BASIC_ID = UUID("aaaaaaaa-0000-4000-8000-000000000001")
EDGE_ID = UUID("bbbbbbbb-0000-4000-8000-000000000002")
BACKSLASH = chr(92)
SAMPLE_CWD = BACKSLASH.join(["C:", "Users", "sample", "Projects", "widget"])
EDGE_CWD = BACKSLASH.join(["C:", "Users", "sample", "Projects", "edge"])

runner = CliRunner()


def _store(root: Path, fixture: str, session_id: UUID, cwd: str) -> Path:
    """A ``.claude`` folder holding one fixture session, laid out as Claude Code does."""
    project = root / "projects" / mangle(cwd)
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{session_id}.jsonl").write_bytes((FIXTURES / fixture).read_bytes())
    return root


def _sessions(root: Path) -> int:
    return len(list((root / "projects").rglob("*.jsonl")))


def _said(result: object) -> str:
    """The output with its wrapping undone, so a sentence can be looked for whole."""
    return " ".join(str(getattr(result, "output", "")).split())


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Claude Code, as the registry sees it, holding one conversation."""
    root = _store(tmp_path / "source", "basic.jsonl", BASIC_ID, SAMPLE_CWD)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    return root


@pytest.fixture
def target(tmp_path: Path) -> Path:
    """A second Claude Code store, already in use, to import into."""
    return _store(tmp_path / "target", "edge.jsonl", EDGE_ID, EDGE_CWD)


@pytest.fixture
def exported(source: Path, tmp_path: Path) -> Path:
    out = tmp_path / "bundle"
    result = runner.invoke(app, ["export", "--tool", "claude-code", "--output", str(out)])
    assert result.exit_code == OK, result.output
    return out


def _foreign_bundle(root: Path, manifest: Manifest, *items: Conversation) -> Path:
    made = Bundle.create(root, manifest)
    for item in items:
        made.add_conversation(item)
    return root


# ---------------------------------------------------------------- export


def test_export_writes_a_bundle_without_asking_anything(exported: Path) -> None:
    assert len(Bundle.open(exported).list_conversations()) == 1


def test_export_refuses_a_folder_that_already_holds_something(source: Path, tmp_path: Path) -> None:
    out = tmp_path / "busy"
    out.mkdir()
    (out / "keep.txt").write_text("mine", encoding="utf-8")

    refused = runner.invoke(app, ["export", "-t", "claude-code", "-o", str(out)])
    assert refused.exit_code == REFUSED
    assert "--force" in _said(refused)
    assert sorted(p.name for p in out.iterdir()) == ["keep.txt"]

    added = runner.invoke(app, ["export", "-t", "claude-code", "-o", str(out), "--force"])
    assert added.exit_code == OK, added.output
    assert (out / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_export_of_a_tool_that_is_not_here_fails_and_says_so() -> None:
    result = runner.invoke(app, ["export", "--tool", "codex"])
    assert result.exit_code == FAILED
    assert "OpenAI Codex was not found" in _said(result) or "was not found" in _said(result)


def test_an_unknown_tool_is_rejected_with_the_valid_names() -> None:
    result = runner.invoke(app, ["export", "--tool", "chatgpt"])
    assert result.exit_code == 2
    assert "claude-code" in result.output


def test_replace_without_encrypt_is_refused(source: Path, tmp_path: Path) -> None:
    out = tmp_path / "b"
    result = runner.invoke(app, ["export", "-t", "claude-code", "-o", str(out), "--replace"])
    assert result.exit_code == REFUSED
    assert not out.exists()


def test_encrypt_without_a_passphrase_is_refused_before_anything_is_exported(
    source: Path, tmp_path: Path
) -> None:
    out = tmp_path / "b"
    result = runner.invoke(app, ["export", "-t", "claude-code", "-o", str(out), "--encrypt"])
    assert result.exit_code == REFUSED
    assert "FERRY_PASSPHRASE" in _said(result)
    assert not out.exists()


def test_encrypt_with_replace_leaves_only_the_sealed_file(source: Path, tmp_path: Path) -> None:
    out = tmp_path / "b"
    result = runner.invoke(
        app,
        ["export", "-t", "claude-code", "-o", str(out), "--encrypt", "--replace"],
        env={"FERRY_PASSPHRASE": "correct horse"},
    )
    assert result.exit_code == OK, result.output
    assert not out.exists()
    assert is_sealed(tmp_path / "b.ferry")


def test_encrypt_will_not_overwrite_a_sealed_file_without_force(
    source: Path, tmp_path: Path
) -> None:
    (tmp_path / "b.ferry").write_bytes(b"someone else's")
    result = runner.invoke(
        app,
        ["export", "-t", "claude-code", "-o", str(tmp_path / "b"), "--encrypt"],
        env={"FERRY_PASSPHRASE": "pw"},
    )
    assert result.exit_code == REFUSED
    assert (tmp_path / "b.ferry").read_bytes() == b"someone else's"


# ---------------------------------------------------------------- import


def test_import_writes_into_the_target(
    exported: Path, target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(target))
    before = _sessions(target)
    result = runner.invoke(app, ["import", "--bundle", str(exported), "--tool", "claude-code"])
    assert result.exit_code == OK, result.output
    assert _sessions(target) == before + 1


def test_dry_run_writes_nothing(
    exported: Path, target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(target))
    before = sorted((target / "projects").rglob("*"))
    result = runner.invoke(app, ["import", "-b", str(exported), "-t", "claude-code", "--dry-run"])
    assert result.exit_code == OK, result.output
    assert "Nothing below is written" in _said(result)
    assert sorted((target / "projects").rglob("*")) == before


def test_a_second_import_keeps_the_copy_already_there_by_default(
    exported: Path, target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(target))
    args = ["import", "-b", str(exported), "-t", "claude-code"]
    assert runner.invoke(app, args).exit_code == OK
    written = {p: p.read_bytes() for p in (target / "projects").rglob("*.jsonl")}

    again = runner.invoke(app, args)
    assert again.exit_code == OK, again.output
    assert {p: p.read_bytes() for p in (target / "projects").rglob("*.jsonl")} == written


def test_a_sealed_bundle_opens_with_the_passphrase_and_not_without(
    source: Path, target: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "b"
    sealed = runner.invoke(
        app,
        ["export", "-t", "claude-code", "-o", str(out), "--encrypt", "--replace"],
        env={"FERRY_PASSPHRASE": "pw"},
    )
    assert sealed.exit_code == OK, sealed.output
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(target))
    args = ["import", "-b", str(tmp_path / "b.ferry"), "-t", "claude-code"]

    unasked = runner.invoke(app, args)
    assert unasked.exit_code == REFUSED
    assert "FERRY_PASSPHRASE" in _said(unasked)

    wrong = runner.invoke(app, [*args, "--passphrase", "not it"])
    assert wrong.exit_code == REFUSED

    before = _sessions(target)
    right = runner.invoke(app, args, env={"FERRY_PASSPHRASE": "pw"})
    assert right.exit_code == OK, right.output
    assert _sessions(target) == before + 1


def test_a_path_that_is_not_a_bundle_is_refused(tmp_path: Path, target: Path) -> None:
    result = runner.invoke(app, ["import", "-b", str(tmp_path / "nowhere"), "-t", "claude-code"])
    assert result.exit_code == REFUSED
    assert "There is nothing at" in _said(result)


def test_a_conversation_not_in_the_bundle_is_refused(
    exported: Path, target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(target))
    before = _sessions(target)
    stray = str(uuid4())
    result = runner.invoke(
        app, ["import", "-b", str(exported), "-t", "claude-code", "--conversation", stray]
    )
    assert result.exit_code == REFUSED
    assert stray in _said(result)
    assert _sessions(target) == before


@pytest.mark.parametrize(
    "flags",
    [
        ["--path-remap", "no-equals-sign"],
        ["--path-remap", "=only-the-new-half"],
        ["--on-conflict", "merge"],
        ["--mode", "summarise"],
    ],
)
def test_a_malformed_flag_is_refused(
    flags: list[str], exported: Path, target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(target))
    before = _sessions(target)
    result = runner.invoke(app, ["import", "-b", str(exported), "-t", "claude-code", *flags])
    assert result.exit_code == 2
    assert _sessions(target) == before


def test_cross_tool_is_refused_without_the_flag_naming_both_tools(
    tmp_path: Path,
    target: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest: Manifest,
    conversation: Conversation,
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(target))
    foreign = conversation.model_copy(update={"source_tool": "codex"})
    bundle = _foreign_bundle(tmp_path / "foreign", manifest, foreign)
    before = _sessions(target)

    refused = runner.invoke(app, ["import", "-b", str(bundle), "-t", "claude-code"])
    assert refused.exit_code == REFUSED
    said = _said(refused)
    assert "codex" in said
    assert "Claude Code" in said
    assert "--allow-cross-tool" in said
    assert _sessions(target) == before

    allowed = runner.invoke(
        app, ["import", "-b", str(bundle), "-t", "claude-code", "--allow-cross-tool"]
    )
    assert allowed.exit_code == OK, allowed.output
    assert "Converting costs" in _said(allowed)
    assert _sessions(target) == before + 1


def test_a_mixed_bundle_imports_its_own_and_skips_the_rest_without_the_flag(
    tmp_path: Path,
    target: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest: Manifest,
    conversation: Conversation,
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(target))
    foreign = conversation.model_copy(update={"id": uuid4(), "source_tool": "codex"})
    bundle = _foreign_bundle(tmp_path / "mixed", manifest, conversation, foreign)
    before = _sessions(target)

    result = runner.invoke(app, ["import", "-b", str(bundle), "-t", "claude-code"])
    assert result.exit_code == OK, result.output
    assert "will be skipped" in _said(result)
    assert _sessions(target) == before + 1


def test_a_bundle_from_another_home_suggests_the_remap(
    tmp_path: Path,
    target: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest: Manifest,
    conversation: Conversation,
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(target))
    bundle = _foreign_bundle(tmp_path / "moved", manifest, conversation)
    result = runner.invoke(app, ["import", "-b", str(bundle), "-t", "claude-code", "--dry-run"])
    assert result.exit_code == OK, result.output
    assert "--path-remap" in _said(result)


# ---------------------------------------------------------------- remove


@pytest.fixture
def imported(
    tmp_path: Path,
    target: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest: Manifest,
    conversation: Conversation,
) -> Path:
    """One conversation Ferry converted into the target store, recorded as its own."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(target))
    foreign = conversation.model_copy(update={"source_tool": "codex"})
    bundle = _foreign_bundle(tmp_path / "foreign", manifest, foreign)
    result = runner.invoke(
        app, ["import", "-b", str(bundle), "-t", "claude-code", "--allow-cross-tool"]
    )
    assert result.exit_code == OK, result.output
    return target


def _record() -> str:
    (record,) = list(provenance.recorded("claude-code"))
    return str(record)


def _written(record: str) -> Path:
    written = provenance.written_file("claude-code", UUID(record))
    assert written is not None
    return Path(written.path)


def test_remove_naming_nothing_lists_and_deletes_nothing(imported: Path) -> None:
    record = _record()
    result = runner.invoke(app, ["remove", "--tool", "claude-code"])
    assert result.exit_code == REFUSED
    assert record in _said(result)
    assert _written(record).exists()


def test_remove_dry_run_deletes_nothing(imported: Path) -> None:
    record = _record()
    result = runner.invoke(app, ["remove", "-t", "claude-code", "--all", "--dry-run"])
    assert result.exit_code == OK, result.output
    assert "Nothing below is deleted" in _said(result)
    assert _written(record).exists()


def test_remove_deletes_what_is_named_and_backs_it_up(imported: Path) -> None:
    record = _record()
    path = _written(record)
    result = runner.invoke(app, ["remove", "-t", "claude-code", "--conversation", record])
    assert result.exit_code == OK, result.output
    assert not path.exists()
    assert list(backup_root().rglob(path.name))
    assert list(provenance.recorded("claude-code")) == []


def test_remove_refuses_an_id_ferry_did_not_import(imported: Path) -> None:
    stray = str(uuid4())
    result = runner.invoke(app, ["remove", "-t", "claude-code", "-c", stray])
    assert result.exit_code == REFUSED
    assert stray in _said(result)
    assert _written(_record()).exists()


def test_remove_refuses_all_and_a_name_together(imported: Path) -> None:
    result = runner.invoke(app, ["remove", "-t", "claude-code", "--all", "-c", _record()])
    assert result.exit_code == REFUSED
    assert _written(_record()).exists()


def test_remove_refuses_while_the_app_is_open(
    imported: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(REGISTRY["claude-code"], "in_use", lambda: "Claude Code is open.")
    result = runner.invoke(app, ["remove", "-t", "claude-code", "--all"])
    assert result.exit_code == REFUSED
    assert "Claude Code is open." in _said(result)
    assert _written(_record()).exists()


def test_remove_with_nothing_imported_says_so(
    target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(target))
    result = runner.invoke(app, ["remove", "-t", "claude-code", "--all"])
    assert result.exit_code == OK
    assert "nothing it may delete" in _said(result)


# ---------------------------------------------------------------- the wordmark


def test_a_piped_run_prints_no_wordmark(source: Path, tmp_path: Path) -> None:
    """A script reads this output; box-drawing in front of it is in the way."""
    result = runner.invoke(app, ["export", "-t", "claude-code", "-o", str(tmp_path / "b")])
    assert result.exit_code == OK, result.output
    assert TAGLINE not in result.output
    assert "F E R R Y" not in result.output


@pytest.mark.parametrize("run", [export_bundle, import_bundle])
def test_a_run_in_a_terminal_opens_with_the_wordmark(
    run: Callable[..., int], capsys: pytest.CaptureFixture[str]
) -> None:
    # A terminal without colour: interactive, but the ASCII form, so the
    # assertion does not depend on this console's encoding.
    ui = UI(THEMES["mono"], capability=Capability.NO_COLOR)
    kwargs = {"bundle": "nowhere"} if run is import_bundle else {}
    run(ui, tool="codex", **kwargs)
    shown = capsys.readouterr().out
    assert TAGLINE in shown
    assert "F E R R Y" in shown


# ---------------------------------------------------------------- the hints


_UNBUILT_HINTS = {"--into"}
"""Flags the screens name that no command takes yet.

``--into`` is where the inspect screen unseals a bundle to. There is no
``ferry inspect`` command for it to belong to; when one arrives, this empties.
"""


def test_every_flag_a_screen_suggests_exists() -> None:
    """A prompt that cannot be shown names a flag. It must be one that works."""
    flows = Path(__file__).parents[1] / "src" / "ferry" / "cli" / "flows.py"
    suggested = set(
        re.findall(
            r"--[a-z][a-z-]+",
            " ".join(re.findall(r'hint="use ([^"]+)"', flows.read_text(encoding="utf-8"))),
        )
    )
    command = typer.main.get_command(app)
    offered = {
        option
        for sub in getattr(command, "commands", {}).values()
        for param in sub.params
        for option in param.opts
    }
    assert suggested - offered == _UNBUILT_HINTS
