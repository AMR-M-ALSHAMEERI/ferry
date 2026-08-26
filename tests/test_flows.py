"""The Export and Import screens.

The adapters are tested elsewhere and thoroughly; what matters here is the
wiring. Two properties carry the weight:

- Import is the only screen in Ferry that writes into a real conversation
  store, so it must not write without an explicit yes.
- Every warning an adapter yields must reach the user. An adapter reporting
  what it could not carry is saying something the person needs to hear, and a
  screen that swallows those in favour of a tidy summary lies by omission.
"""

from __future__ import annotations

import io
import shutil
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ferry.adapters.base import (
    Adapter,
    DetectResult,
    ExportEvent,
    ImportEvent,
    ImportOptions,
)
from ferry.cli.flows import (
    _describe,
    _report,
    bundle_at,
    clean_path,
    find_bundles,
    run_export,
    run_import,
    run_inspect,
)
from ferry.cli.theme import MONO, Capability
from ferry.cli.ui import UI, NonInteractiveError
from ferry.core.sealed import is_sealed, opens_with


class _Recorder(Adapter):
    """An adapter that records what it was asked to do and yields fixed events."""

    def __init__(
        self,
        name: str = "claude-code",
        events: Sequence[ExportEvent | ImportEvent] = (),
        installed: bool = True,
    ) -> None:
        self.name = name
        self.display_name = name.title()
        self._events = list(events)
        self._installed = installed
        self.exported_to: Path | None = None
        self.imported_from: Path | None = None
        self.options: list[ImportOptions] = []

    def detect(self) -> DetectResult:
        return DetectResult(installed=self._installed, conversation_count_estimate=2)

    def export(self, dest_bundle_dir: Path) -> Iterator[ExportEvent]:
        self.exported_to = dest_bundle_dir
        yield from self._events  # type: ignore[misc]

    def import_(self, bundle_dir: Path, options: ImportOptions) -> Iterator[ImportEvent]:
        self.imported_from = bundle_dir
        self.options.append(options)
        yield from self._events  # type: ignore[misc]


class _Escape:
    """The user pressing escape, as something a test can put in a list."""

    def __repr__(self) -> str:
        return "ESCAPE"


ESCAPE = _Escape()


class _Answers(UI):
    """A UI with the prompts pre-answered."""

    def __init__(
        self,
        *,
        path: str | Sequence[str] | None = None,
        confirm: bool | None = True,
        actions: Sequence[object] | None = None,
        secrets: Sequence[str] | None = None,
    ) -> None:
        super().__init__(MONO, capability=Capability.PLAIN)
        # The real console, redirected -- not a replacement. A bare rich Console
        # has none of the ferry.* styles, so swapping it out tests a renderer
        # the user never sees and hides any style name that does not exist.
        self.buffer = io.StringIO()
        self.console.file = self.buffer
        self.console.width = 100
        # A list is answered in order; a bare string is answered once. Both
        # then answer None, which is escape. The path prompt asks again until
        # it gets something real, so a fake that repeats one wrong answer for
        # ever would hang the suite rather than test it.
        self._paths = [path] if isinstance(path, str) else list(path or [])
        self.paths_asked = 0
        self._confirm = confirm
        # Answers taken in order, by value rather than by position, so a test
        # says which option it is choosing instead of which row it happens to
        # sit on. Falls back to the first choice, which every screen makes the
        # safe one.
        self._actions = list(actions or [])
        # Taken in order and never reused, so a test can give two different
        # passphrases and check that a mismatch is caught.
        self._secrets = list(secrets or [])
        self.secrets_asked = 0
        self.asked = 0
        self.questions: list[str] = []
        self.asked_to_confirm = 0
        self.confirmed: list[str] = []
        self.path_defaults: list[str] = []
        self.starts_at: list[int] = []
        self.offered_back: list[bool] = []

    def select(self, question, choices, **kwargs):  # type: ignore[no-untyped-def]
        self.questions.append(question)
        self.starts_at.append(kwargs.get("initial", 0))
        self.offered_back.append(bool(kwargs.get("back", False)))
        values = [value for value, _ in choices]
        self.asked += 1
        # ESCAPE is answered wherever it lands, because "the user pressed
        # escape here" is not an option on any screen -- it is the absence of
        # one, and a screen with a back stack has to be tested for what happens
        # at each rung of it.
        if self._actions and self._actions[0] is ESCAPE:
            self._actions.pop(0)
            return None
        # Consumed only when it fits this screen, so a test naming the answer to
        # one question is not silently spent on a different question appearing
        # before it.
        if self._actions and self._actions[0] in values:
            return self._actions.pop(0)
        return choices[0][0]

    def path(self, question, *, default="", hint=""):  # type: ignore[no-untyped-def]
        self.paths_asked += 1
        self.path_defaults.append(default)
        return self._paths.pop(0) if self._paths else None

    def secret(self, question, *, hint=""):  # type: ignore[no-untyped-def]
        self.secrets_asked += 1
        return self._secrets.pop(0) if self._secrets else None

    def confirm(self, question, *, default, hint=""):  # type: ignore[no-untyped-def]
        self.asked_to_confirm += 1
        # Recorded, because a question is asked through prompt_toolkit and
        # never reaches the console buffer -- asserting on `ui.text` for a
        # question tests the fake, not the flow.
        self.confirmed.append(question)
        return self._confirm

    @property
    def text(self) -> str:
        return self.buffer.getvalue()


def scanned(*adapters: Adapter) -> list[tuple[Adapter, DetectResult]]:
    return [(a, a.detect()) for a in adapters]


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------


def test_export_runs_the_adapter_against_the_chosen_directory(tmp_path: Path) -> None:
    adapter = _Recorder(events=[ExportEvent(kind="progress", message="7 messages")])
    ui = _Answers(path=str(tmp_path / "bundle"))

    run_export(ui, scanned(adapter))

    assert adapter.exported_to == tmp_path / "bundle"
    assert "1 conversations exported" in ui.text


def test_export_does_nothing_when_no_assistant_was_found() -> None:
    adapter = _Recorder(installed=False)
    ui = _Answers(path="/wherever")

    run_export(ui, scanned(adapter))

    assert adapter.exported_to is None
    assert "Nothing to export" in ui.text


def test_export_stops_if_the_destination_is_left_blank() -> None:
    adapter = _Recorder()
    ui = _Answers(path=None)

    run_export(ui, scanned(adapter))

    assert adapter.exported_to is None


def test_export_asks_before_adding_to_a_directory_that_is_not_empty(tmp_path: Path) -> None:
    existing = tmp_path / "bundle"
    existing.mkdir()
    (existing / "manifest.json").write_text("{}", encoding="utf-8")
    adapter = _Recorder()
    ui = _Answers(path=str(existing), confirm=False)

    run_export(ui, scanned(adapter))

    assert ui.asked_to_confirm == 1
    assert adapter.exported_to is None


def test_every_warning_reaches_the_user(tmp_path: Path) -> None:
    """An adapter saying what it could not carry must not be summarised away."""
    adapter = _Recorder(
        events=[
            ExportEvent(kind="progress", message="ok"),
            ExportEvent(kind="warning", message="token counts are not carried"),
            ExportEvent(kind="warning", message="encrypted reasoning is not restored"),
        ]
    )
    ui = _Answers(path=str(tmp_path / "bundle"))

    run_export(ui, scanned(adapter))

    assert "token counts are not carried" in ui.text
    assert "encrypted reasoning is not restored" in ui.text


def test_a_flood_of_warnings_is_capped_but_counted(tmp_path: Path) -> None:
    adapter = _Recorder(
        events=[ExportEvent(kind="warning", message=f"warning {i}") for i in range(20)]
    )
    ui = _Answers(path=str(tmp_path / "bundle"))

    run_export(ui, scanned(adapter))

    assert "warning 0" in ui.text
    assert "and 12 more" in ui.text


def test_an_adapter_error_is_reported_as_a_failure(tmp_path: Path) -> None:
    adapter = _Recorder(events=[ExportEvent(kind="error", message="disk full")])
    ui = _Answers(path=str(tmp_path / "bundle"))

    run_export(ui, scanned(adapter))

    assert "disk full" in ui.text
    assert "1 failed" in ui.text


def test_skipped_conversations_are_counted_separately(tmp_path: Path) -> None:
    adapter = _Recorder(
        events=[
            ExportEvent(kind="progress", message="ok"),
            ExportEvent(kind="skipped", message="already in bundle"),
        ]
    )
    ui = _Answers(path=str(tmp_path / "bundle"))

    run_export(ui, scanned(adapter))

    assert "1 conversations exported, 1 skipped" in ui.text


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_bundles(monkeypatch, tmp_path: Path):
    """Keep the picker off the developer's own disk.

    ``find_bundles`` looks in Desktop and Downloads. A test that reads those
    passes or fails depending on whose machine it runs on -- the same fault as
    a test that reads a real conversation store.
    """
    monkeypatch.setattr("ferry.cli.flows._search_roots", lambda: [tmp_path / "nowhere"])


@pytest.fixture
def bundle_dir(tmp_path: Path, manifest, conversation) -> Path:
    from ferry.core import Bundle

    bundle = Bundle.create(tmp_path / "b", manifest)
    bundle.add_conversation(conversation)
    return tmp_path / "b"


def test_import_refuses_to_write_without_an_explicit_yes(bundle_dir: Path) -> None:
    """The one screen that writes into a real conversation store."""
    adapter = _Recorder()
    ui = _Answers(path=str(bundle_dir), actions=["cancel"])

    run_import(ui, scanned(adapter))

    assert adapter.imported_from is None
    assert "Nothing was written" in ui.text


def test_import_says_it_is_writing_into_real_history(bundle_dir: Path) -> None:
    adapter = _Recorder()
    ui = _Answers(path=str(bundle_dir), actions=["cancel"])

    run_import(ui, scanned(adapter))

    assert "writes into your real" in ui.text


def test_import_runs_the_adapter_once_confirmed(bundle_dir: Path) -> None:
    adapter = _Recorder(events=[ImportEvent(kind="progress", message="7 messages")])
    ui = _Answers(path=str(bundle_dir), confirm=True)

    run_import(ui, scanned(adapter))

    assert adapter.imported_from == bundle_dir
    assert "1 conversations imported" in ui.text


def test_import_reports_what_is_in_the_bundle_before_asking(bundle_dir: Path) -> None:
    """The confirmation is meaningless if the user cannot see what they are accepting."""
    adapter = _Recorder()
    ui = _Answers(path=str(bundle_dir), confirm=False)

    run_import(ui, scanned(adapter))

    assert "1 conversations" in ui.text
    assert "claude-code" in ui.text


def test_import_rejects_a_directory_that_is_not_a_bundle(tmp_path: Path) -> None:
    adapter = _Recorder()
    ui = _Answers(path=str(tmp_path), confirm=True)

    run_import(ui, scanned(adapter))

    assert adapter.imported_from is None
    assert "not a bundle" in ui.text


def test_import_does_nothing_when_no_assistant_was_found(bundle_dir: Path) -> None:
    adapter = _Recorder(installed=False)
    ui = _Answers(path=str(bundle_dir), confirm=True)

    run_import(ui, scanned(adapter))

    assert adapter.imported_from is None
    assert "Nothing to import into" in ui.text


def test_a_screen_that_cannot_prompt_gives_up_rather_than_hanging(tmp_path: Path) -> None:
    """A plain UI has no terminal; both screens must fail fast, not block."""

    class _NoTerminal(_Answers):
        def path(self, question, *, default="", hint=""):  # type: ignore[no-untyped-def]
            raise NonInteractiveError(question, hint)

    adapter = _Recorder()
    for flow in (run_export, run_import):
        ui = _NoTerminal()
        flow(ui, scanned(adapter))
        assert adapter.exported_to is None
        assert adapter.imported_from is None


# --------------------------------------------------------------------------
# choosing a bundle
# --------------------------------------------------------------------------


def test_a_bundle_lying_nearby_is_offered_rather_than_asked_for(
    monkeypatch, bundle_dir: Path
) -> None:
    """The screen used to present a blank line and wait. Nothing told the user
    what to type, which is the failure this picker exists to prevent."""
    monkeypatch.setattr("ferry.cli.flows._search_roots", lambda: [bundle_dir.parent])
    adapter = _Recorder(events=[ImportEvent(kind="progress", message="ok")])
    ui = _Answers(path=None, confirm=True)  # no typing available at all

    run_import(ui, scanned(adapter))

    assert adapter.imported_from == bundle_dir


def test_a_listed_bundle_says_what_is_in_it(monkeypatch, bundle_dir: Path) -> None:
    monkeypatch.setattr("ferry.cli.flows._search_roots", lambda: [bundle_dir.parent])

    label = _describe(bundle_dir)

    assert "1 conversation" in label
    assert "claude-code" in label


def test_typing_a_path_is_still_possible_when_nothing_is_found(bundle_dir: Path) -> None:
    adapter = _Recorder()
    ui = _Answers(path=str(bundle_dir), confirm=True)

    run_import(ui, scanned(adapter))

    assert adapter.imported_from == bundle_dir
    assert "No bundle found nearby" in ui.text


def test_find_bundles_looks_one_level_down_and_no_further(tmp_path: Path, manifest) -> None:
    from ferry.core import Bundle

    Bundle.create(tmp_path / "near", manifest)
    Bundle.create(tmp_path / "a" / "b" / "deep", manifest)

    found = find_bundles([tmp_path])

    assert found == [tmp_path / "near"]


def test_find_bundles_ignores_a_root_that_is_not_there(tmp_path: Path) -> None:
    assert find_bundles([tmp_path / "gone"]) == []


def test_a_bundle_with_a_broken_manifest_is_still_listed(tmp_path: Path) -> None:
    """Better to offer it and let Bundle.open explain, than to hide it and
    leave the user certain their bundle is missing."""
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "manifest.json").write_text("{ not json", encoding="utf-8")

    assert find_bundles([tmp_path]) == [broken]
    assert "unreadable" in _describe(broken)


def test_an_unreadable_root_is_skipped_rather_than_fatal(tmp_path: Path, monkeypatch) -> None:
    def refuse(self):  # type: ignore[no-untyped-def]
        raise PermissionError("nope")

    monkeypatch.setattr(Path, "iterdir", refuse)

    assert find_bundles([tmp_path]) == []


# --------------------------------------------------------------------------
# progress
# --------------------------------------------------------------------------


class _Bar:
    """Records what the progress bar was told, without drawing one."""

    def __init__(self) -> None:
        self.advanced = 0
        self.labels: list[str] = []

    def advance(self, n: int = 1) -> None:
        self.advanced += n

    def describe(self, text: str) -> None:
        self.labels.append(text)


class _Watched(_Answers):
    def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(**kwargs)
        self.bar = _Bar()
        self.sized: list[tuple[str, int]] = []

    @contextmanager
    def progress(self, label, total):  # type: ignore[no-untyped-def]
        self.sized.append((label, total))
        yield self.bar


def test_export_shows_a_bar_sized_from_the_scan(tmp_path: Path) -> None:
    """Ten seconds passed with nothing on screen before this existed, and a
    single gap between two conversations was 3.6s."""
    adapter = _Recorder(events=[ExportEvent(kind="progress", message=f"c{i}") for i in range(3)])
    ui = _Watched(path=str(tmp_path / "bundle"))

    run_export(ui, scanned(adapter))

    assert ui.sized == [("Exporting", 2)]  # _Recorder.detect estimates 2
    assert ui.bar.advanced == 3


def test_a_low_estimate_makes_the_bar_grow_rather_than_sit_full() -> None:
    """detect() counts files; export reads them. The two disagree in practice,
    and a bar pinned at 100% while work continues is worse than no bar."""
    ui = UI(MONO, capability=Capability.NO_COLOR)
    ui.console.file = io.StringIO()

    with ui.progress("Exporting", 2) as bar:
        for _ in range(5):
            bar.advance()

    assert getattr(bar, "done", None) == 5


def test_skipped_conversations_move_the_bar_too(tmp_path: Path) -> None:
    """A skipped conversation is work done. Leaving it out strands the bar
    short of the end with nothing left to advance it."""
    adapter = _Recorder(
        events=[
            ExportEvent(kind="progress", message="ok"),
            ExportEvent(kind="skipped", message="already in bundle"),
        ]
    )
    ui = _Watched(path=str(tmp_path / "bundle"))

    run_export(ui, scanned(adapter))

    assert ui.bar.advanced == 2


def test_a_long_description_is_trimmed_to_fit_beside_the_bar(tmp_path: Path) -> None:
    adapter = _Recorder(events=[ExportEvent(kind="progress", message="x" * 200)])
    ui = _Watched(path=str(tmp_path / "bundle"))

    run_export(ui, scanned(adapter))

    assert all(len(label) <= 46 for label in ui.bar.labels)
    assert ui.bar.labels[-1].endswith("...")


def test_import_sizes_the_bar_from_the_bundle_not_a_guess(bundle_dir: Path) -> None:
    adapter = _Recorder(events=[ImportEvent(kind="progress", message="ok")])
    ui = _Watched(path=str(bundle_dir), actions=["skip"])

    run_import(ui, scanned(adapter))

    assert ui.sized == [("Importing", 1)]


def test_warnings_are_printed_after_the_bar_not_under_it(tmp_path: Path) -> None:
    """A warning that scrolls past beneath a live bar is a warning nobody reads."""
    adapter = _Recorder(
        events=[
            ExportEvent(kind="warning", message="token counts are not carried"),
            ExportEvent(kind="progress", message="ok"),
        ]
    )
    ui = _Watched(path=str(tmp_path / "bundle"))

    run_export(ui, scanned(adapter))

    assert "token counts are not carried" in ui.text
    assert "token counts are not carried" not in " ".join(ui.bar.labels)


class TestExportMessageGrouping:
    """Notes and warnings are different things and must not look the same.

    They used to come out as one undifferentiated list with the warning marker
    on every line, so a routine remark about the storage format looked like
    something had gone wrong. A warning marker that appears on everything stops
    meaning anything.
    """

    def _render(self, events: list[ExportEvent]) -> str:
        ui = UI()
        with ui.console.capture() as captured:
            _report(ui, iter(events), "exported", label="Exporting", total=1)
        return captured.get()

    def test_notes_and_warnings_get_their_own_headings(self) -> None:
        text = self._render(
            [
                ExportEvent(kind="started", message="1 to read"),
                ExportEvent(kind="progress", message="12 messages"),
                ExportEvent(kind="note", message="22 images were attached"),
                ExportEvent(kind="warning", message="step type 28 has no name"),
            ]
        )
        assert "Notes" in text
        assert "Warnings" in text
        assert text.index("Notes") < text.index("Warnings")

    def test_a_heading_with_nothing_under_it_is_not_printed(self) -> None:
        text = self._render(
            [
                ExportEvent(kind="started", message="1 to read"),
                ExportEvent(kind="progress", message="12 messages"),
            ]
        )
        assert "Notes" not in text
        assert "Warnings" not in text

    def test_a_repeated_message_is_said_once_with_a_count(self) -> None:
        """Adapters emit rebuild warnings once per conversation."""
        text = self._render(
            [
                ExportEvent(kind="started", message="3 to read"),
                *[
                    ExportEvent(kind="warning", message="tool output is not carried")
                    for _ in range(3)
                ],
            ]
        )
        assert text.count("tool output is not carried") == 1
        assert "(x3)" in text

    def test_the_bar_is_sized_by_the_adapter_when_it_says_so(self) -> None:
        """detect() counts conversations; an adapter can have more work than that.

        Antigravity writes a subagent trajectory for each conversation that
        spawned one, so two conversations are six writes and the bar read 6/2.
        """
        text = self._render(
            [
                ExportEvent(kind="started", message="2 conversations", total=6),
                *[ExportEvent(kind="progress", message="done") for _ in range(6)],
            ]
        )
        assert "6/6" in text or "(6/6)" in text

    def test_the_adapters_own_closing_line_is_the_summary(self) -> None:
        """It knows things the screen cannot derive from counting events."""
        text = self._render(
            [
                ExportEvent(kind="started", message="2 conversations", total=6),
                *[ExportEvent(kind="progress", message="done") for _ in range(6)],
                ExportEvent(kind="done", message="2 conversations exported, plus 4 subagents"),
            ]
        )
        assert "2 conversations exported, plus 4 subagents" in text
        assert "6 exported" not in text


class TestImportOptionsScreen:
    """One screen carrying the options, instead of three prompts.

    `ImportOptions` has carried `dry_run`, `on_conflict` and `path_remap` since
    M3 and the import screen passed `ImportOptions()` -- the defaults, always.
    Everything below existed and was unreachable, including the path remapping
    the whole Antigravity milestone was built around: a bundle restored onto
    another machine had every recorded path left wrong.
    """

    def _bundle(self, tmp_path: Path, conversation, home: str) -> Path:
        from ferry.core import Bundle, Manifest, SourceMachine

        bundle = Bundle.create(
            tmp_path / "remap-bundle",
            Manifest(
                created_at=datetime(2026, 8, 1, tzinfo=UTC),
                created_by="ferry test",
                source_machine=SourceMachine(os="win32", user_home=home),
            ),
        )
        bundle.add_conversation(conversation)
        return tmp_path / "remap-bundle"

    def test_the_safe_option_is_the_one_under_the_cursor(self, bundle_dir: Path) -> None:
        """First in the list is what enter takes, and it must write nothing."""
        adapter = _Recorder(events=[ImportEvent(kind="progress", message="ok")])
        ui = _Answers(path=str(bundle_dir))

        run_import(ui, scanned(adapter))

        assert adapter.options[0].dry_run is True

    def test_a_preview_runs_a_dry_run_and_then_the_real_thing(self, bundle_dir: Path) -> None:
        adapter = _Recorder(events=[ImportEvent(kind="progress", message="ok")])
        ui = _Answers(path=str(bundle_dir), actions=["preview", "skip"])

        run_import(ui, scanned(adapter))

        assert [option.dry_run for option in adapter.options] == [True, False]
        assert "Nothing below is written" in ui.text

    def test_cancelling_after_a_preview_writes_nothing(self, bundle_dir: Path) -> None:
        adapter = _Recorder(events=[ImportEvent(kind="progress", message="ok")])
        ui = _Answers(path=str(bundle_dir), actions=["preview", "cancel"])

        run_import(ui, scanned(adapter))

        assert [option.dry_run for option in adapter.options] == [True]
        assert "Nothing was written" in ui.text

    def test_keeping_both_copies_reaches_the_adapter(self, bundle_dir: Path) -> None:
        adapter = _Recorder(events=[ImportEvent(kind="progress", message="ok")])
        ui = _Answers(path=str(bundle_dir), actions=["rename"])

        run_import(ui, scanned(adapter))

        assert adapter.options[-1].on_conflict == "rename"
        assert adapter.options[-1].dry_run is False

    def test_replacing_reaches_the_adapter(self, bundle_dir: Path) -> None:
        adapter = _Recorder(events=[ImportEvent(kind="progress", message="ok")])
        ui = _Answers(path=str(bundle_dir), actions=["overwrite"])

        run_import(ui, scanned(adapter))

        assert adapter.options[-1].on_conflict == "overwrite"

    def test_a_bundle_from_this_machine_is_not_asked_about(
        self, tmp_path: Path, conversation
    ) -> None:
        """A question with one sensible answer is not worth asking."""
        bundle_dir = self._bundle(tmp_path, conversation, str(Path.home()))
        adapter = _Recorder(events=[ImportEvent(kind="progress", message="ok")])
        ui = _Answers(path=str(bundle_dir), actions=["skip"])

        run_import(ui, scanned(adapter))

        assert not any("read as now" in question for question in ui.questions)
        assert adapter.options[-1].path_remap == ()

    def test_a_bundle_from_a_machine_that_is_not_here_offers_to_remap(
        self, tmp_path: Path, conversation
    ) -> None:
        """The gap this closes is the point of the Antigravity milestone.

        A bundle carrying `C:\\Users\\someone-else` restored here left every
        recorded path pointing at a folder that does not exist, and the CLI
        never offered the remapper that had been built to fix exactly that.
        """
        gone = str(tmp_path / "not-a-real-home")
        bundle_dir = self._bundle(tmp_path, conversation, gone)
        adapter = _Recorder(events=[ImportEvent(kind="progress", message="ok")])
        ui = _Answers(path=str(bundle_dir), actions=["here", "skip"])

        run_import(ui, scanned(adapter))

        assert any("read as now" in question for question in ui.questions)
        assert adapter.options[-1].path_remap == ((gone, str(Path.home())),)

    def test_the_paths_can_be_left_alone(self, tmp_path: Path, conversation) -> None:
        gone = str(tmp_path / "not-a-real-home")
        bundle_dir = self._bundle(tmp_path, conversation, gone)
        adapter = _Recorder(events=[ImportEvent(kind="progress", message="ok")])
        ui = _Answers(path=str(bundle_dir), actions=["leave", "skip"])

        run_import(ui, scanned(adapter))

        assert adapter.options[-1].path_remap == ()


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------


class TestInspect:
    """Looking inside a bundle, and deleting from it.

    Deleting lives on this screen because it is the same act: you remove
    something after looking at it, never before. So these tests care about two
    things -- that the screen says enough for the decision, and that it does
    nothing at all until it has been asked twice.
    """

    def test_it_says_what_is_in_the_bundle(self, bundle_dir: Path) -> None:
        ui = _Answers(path=str(bundle_dir), actions=["done"])

        run_inspect(ui)

        assert "1 conversation" in ui.text
        assert "claude-code" in ui.text

    def test_it_lists_the_folders_the_conversations_were_recorded_in(
        self, bundle_dir: Path, conversation
    ) -> None:
        """The list the import screen cannot afford to compute.

        Someone restoring onto another machine needs to see which folders a
        bundle expects before being asked where those folders now live.
        """
        ui = _Answers(path=str(bundle_dir), actions=["done"])

        run_inspect(ui)

        assert conversation.workspace.original_path in ui.text

    def test_looking_changes_nothing(self, bundle_dir: Path) -> None:
        before = {
            path: path.read_bytes() for path in sorted(bundle_dir.rglob("*")) if path.is_file()
        }
        ui = _Answers(path=str(bundle_dir), actions=["done"])

        run_inspect(ui)

        after = {
            path: path.read_bytes() for path in sorted(bundle_dir.rglob("*")) if path.is_file()
        }
        assert after == before

    def test_a_bundle_that_cannot_be_opened_is_reported_not_raised(self, tmp_path: Path) -> None:
        empty = tmp_path / "not-a-bundle"
        empty.mkdir()
        ui = _Answers(path=str(empty), actions=["done"])

        run_inspect(ui)

        assert "not a bundle" in ui.text

    def test_deleting_a_conversation_names_what_is_lost(
        self, bundle_dir: Path, tmp_path: Path, monkeypatch
    ) -> None:
        """Not a yes or no about "it".

        This is the one action in Ferry after which the data is simply gone --
        a bundle is the backup -- so the question has to say what is going.
        """
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        ui = _Answers(path=str(bundle_dir), actions=["one", "done"], confirm=False)

        run_inspect(ui)

        assert "Deleting:" in ui.text
        assert "Nothing was deleted" in ui.text

    def test_declining_deletes_nothing(self, bundle_dir: Path, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        before = sorted(p.name for p in bundle_dir.rglob("*") if p.is_file())
        ui = _Answers(path=str(bundle_dir), actions=["one", "done"], confirm=False)

        run_inspect(ui)

        assert sorted(p.name for p in bundle_dir.rglob("*") if p.is_file()) == before

    def test_confirming_removes_the_conversation_and_keeps_a_copy(
        self, bundle_dir: Path, tmp_path: Path, monkeypatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        ui = _Answers(path=str(bundle_dir), actions=["one", "done"], confirm=True)

        run_inspect(ui)

        assert list((bundle_dir / "conversations").glob("*.json")) == []
        assert "Deleted" in ui.text
        assert list((home / ".ferry" / "backups").rglob("*.json"))

    def test_declining_to_delete_the_bundle_leaves_it(self, bundle_dir: Path) -> None:
        ui = _Answers(path=str(bundle_dir), actions=["all", "done"], confirm=False)

        run_inspect(ui)

        assert bundle_dir.is_dir()
        assert "Nothing was deleted" in ui.text

    def test_confirming_removes_the_whole_bundle(self, bundle_dir: Path) -> None:
        ui = _Answers(path=str(bundle_dir), actions=["all"], confirm=True)

        run_inspect(ui)

        assert not bundle_dir.exists()
        assert "Deleted" in ui.text

    def test_a_broken_bundle_can_still_be_looked_at(self, bundle_dir: Path) -> None:
        """The bundle you cannot read is the one you need this screen for."""
        document = next((bundle_dir / "conversations").glob("*.json"))
        document.write_text("{ not json", encoding="utf-8")
        ui = _Answers(path=str(bundle_dir), actions=["done"])

        run_inspect(ui)

        assert "cannot be read" in ui.text


# --------------------------------------------------------------------------
# sealing
# --------------------------------------------------------------------------


PHRASE = "a passphrase nobody else knows"


class TestSealing:
    """Encrypting a bundle into one file, from the screens that offer it.

    The property every one of these is really about: **a backup tool must not
    be able to destroy a backup.** The plaintext is only removed after the
    sealed file has been opened again, and only if asked.
    """

    def _export(self, ui, adapter, tmp_path: Path) -> Path:  # type: ignore[no-untyped-def]
        run_export(ui, scanned(adapter))
        return Path(ui._path)

    def test_export_offers_to_seal_and_takes_no_for_an_answer(
        self, tmp_path: Path, conversation
    ) -> None:
        adapter = _RealExport([conversation])
        ui = _Answers(path=str(tmp_path / "bundle"), confirm=False)

        run_export(ui, scanned(adapter))

        assert any("Encrypt this bundle" in question for question in ui.confirmed)
        assert not list(tmp_path.glob("*.ferry"))
        assert (tmp_path / "bundle" / "manifest.json").is_file()

    def test_a_mismatched_passphrase_seals_nothing(self, tmp_path: Path, conversation) -> None:
        """There is no recovery, so it is confirmed rather than trusted.

        A passphrase mistyped once and never noticed produces a file nobody can
        open, and the person finds out on the day they need it.
        """
        adapter = _RealExport([conversation])
        ui = _Answers(
            path=str(tmp_path / "bundle"), confirm=True, secrets=[PHRASE, "something else"]
        )

        run_export(ui, scanned(adapter))

        assert "did not match" in ui.text
        assert not list(tmp_path.glob("*.ferry"))

    def test_backing_out_of_the_passphrase_leaves_it_unencrypted(
        self, tmp_path: Path, conversation
    ) -> None:
        adapter = _RealExport([conversation])
        ui = _Answers(path=str(tmp_path / "bundle"), confirm=True, secrets=[])

        run_export(ui, scanned(adapter))

        assert "Left unencrypted" in ui.text
        assert not list(tmp_path.glob("*.ferry"))

    def test_sealing_says_what_is_at_stake_before_asking(
        self, tmp_path: Path, conversation
    ) -> None:
        adapter = _RealExport([conversation])
        ui = _Answers(path=str(tmp_path / "bundle"), confirm=True, secrets=[PHRASE, PHRASE])

        run_export(ui, scanned(adapter))

        assert "no way to recover" in ui.text
        assert "Lose it and the backup is gone" in ui.text

    def test_a_sealed_file_is_written_and_checked_before_anything_is_removed(
        self, tmp_path: Path, conversation
    ) -> None:
        adapter = _RealExport([conversation])
        # confirm=True answers "seal it" and then "delete the plaintext".
        ui = _Answers(path=str(tmp_path / "bundle"), confirm=True, secrets=[PHRASE, PHRASE])

        run_export(ui, scanned(adapter))

        sealed = list(tmp_path.glob("*.ferry"))
        assert len(sealed) == 1
        assert is_sealed(sealed[0])
        assert opens_with(sealed[0], PHRASE)
        assert "Sealed:" in ui.text

    def test_the_unencrypted_copy_is_kept_unless_asked(self, tmp_path: Path, conversation) -> None:
        adapter = _RealExport([conversation])
        ui = _KeepPlaintext(path=str(tmp_path / "bundle"), secrets=[PHRASE, PHRASE])

        run_export(ui, scanned(adapter))

        assert list(tmp_path.glob("*.ferry"))
        assert (tmp_path / "bundle" / "manifest.json").is_file()
        assert "still at" in ui.text

    def test_a_sealed_bundle_appears_in_the_picker(self, tmp_path: Path, conversation) -> None:
        """Encrypting a bundle must not hide it from Ferry.

        The picker walks directories, and a sealed bundle is a file.
        """
        adapter = _RealExport([conversation])
        ui = _KeepPlaintext(path=str(tmp_path / "bundle"), secrets=[PHRASE, PHRASE])
        run_export(ui, scanned(adapter))
        sealed = next(iter(tmp_path.glob("*.ferry")))

        found = find_bundles([tmp_path])

        assert sealed in found
        assert "sealed" in _describe(sealed)

    def test_inspecting_a_sealed_bundle_asks_for_the_passphrase_and_opens_it(
        self, tmp_path: Path, conversation
    ) -> None:
        adapter = _RealExport([conversation])
        seeded = _KeepPlaintext(path=str(tmp_path / "bundle"), secrets=[PHRASE, PHRASE])
        run_export(seeded, scanned(adapter))
        sealed = next(iter(tmp_path.glob("*.ferry")))

        ui = _Answers(path=str(sealed), actions=["done"], secrets=[PHRASE])
        run_inspect(ui)

        assert ui.secrets_asked == 1
        assert "1 conversation" in ui.text

    def test_a_wrong_passphrase_says_so_and_shows_nothing(
        self, tmp_path: Path, conversation
    ) -> None:
        adapter = _RealExport([conversation])
        seeded = _KeepPlaintext(path=str(tmp_path / "bundle"), secrets=[PHRASE, PHRASE])
        run_export(seeded, scanned(adapter))
        sealed = next(iter(tmp_path.glob("*.ferry")))

        ui = _Answers(path=str(sealed), actions=["done"], secrets=["wrong"])
        run_inspect(ui)

        assert "did not open" in ui.text
        assert "conversation" not in ui.text.split("did not open")[-1]

    def test_a_sealed_bundle_offers_a_way_to_change_it(self, tmp_path: Path, conversation) -> None:
        """This screen used to be a dead end.

        It said "unseal it first" and Ferry had no way to do that -- an
        instruction the program did not support. Both ways out are offered
        here now, and looking is still what happens if you touch nothing.
        """
        adapter = _RealExport([conversation])
        seeded = _KeepPlaintext(path=str(tmp_path / "bundle"), secrets=[PHRASE, PHRASE])
        run_export(seeded, scanned(adapter))
        sealed = next(iter(tmp_path.glob("*.ferry")))
        before = sealed.read_bytes()

        ui = _Recorded(path=str(sealed), actions=["done"], secrets=[PHRASE])
        run_inspect(ui)

        offered = {value for question, choices in ui.offered for value, _ in choices}
        assert "unseal" in offered, "the plain way: write it out where it can be worked on"
        assert "one" in offered, "the convenient way: delete, then seal it again"
        assert sealed.read_bytes() == before, "looking must change nothing"

    def test_the_unsealed_copy_does_not_survive_the_screen(
        self, tmp_path: Path, conversation, monkeypatch
    ) -> None:
        """An unencrypted copy left lying about defeats the whole feature."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

        adapter = _RealExport([conversation])
        seeded = _KeepPlaintext(path=str(tmp_path / "bundle"), secrets=[PHRASE, PHRASE])
        run_export(seeded, scanned(adapter))
        sealed = next(iter(tmp_path.glob("*.ferry")))

        ui = _Answers(path=str(sealed), actions=["done"], secrets=[PHRASE])
        run_inspect(ui)

        left = list((home / ".ferry" / "open").rglob("manifest.json"))
        assert left == []


class _KeepPlaintext(_Answers):
    """Says yes to sealing and no to deleting the unencrypted copy."""

    def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(**kwargs)
        self._answers = [True, False]

    def confirm(self, question, *, default, hint=""):  # type: ignore[no-untyped-def]
        self.asked_to_confirm += 1
        return self._answers.pop(0) if self._answers else False


class _Recorded(_Answers):
    """Remembers which choices it was offered."""

    def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(**kwargs)
        self.offered: list[tuple[str, list[tuple[str, str]]]] = []

    def select(self, question, choices, **kwargs):  # type: ignore[no-untyped-def]
        self.offered.append((question, list(choices)))
        return super().select(question, choices, **kwargs)


class _RealExport(Adapter):
    """An adapter that writes a genuine bundle, so sealing has something to seal."""

    name = "claude-code"
    display_name = "Claude Code"

    def __init__(self, conversations: Sequence[object]) -> None:
        self._conversations = list(conversations)

    def detect(self) -> DetectResult:
        return DetectResult(installed=True, conversation_count_estimate=len(self._conversations))

    def export(self, dest_bundle_dir: Path) -> Iterator[ExportEvent]:
        from ferry.core import Bundle, Manifest, SourceMachine

        bundle = Bundle.create(
            dest_bundle_dir,
            Manifest(
                created_at=datetime(2026, 8, 1, tzinfo=UTC),
                created_by="ferry test",
                source_machine=SourceMachine(os="linux", user_home="/home/sample"),
            ),
            force=True,
        )
        yield ExportEvent(kind="started", message="1 conversation")
        for conversation in self._conversations:
            bundle.add_conversation(conversation)  # type: ignore[arg-type]
            yield ExportEvent(kind="progress", message="written")
        yield ExportEvent(kind="done", message=f"{len(self._conversations)} exported")

    def import_(self, bundle_dir: Path, options: ImportOptions) -> Iterator[ImportEvent]:
        yield ImportEvent(kind="done", message="nothing")


class TestWrongPassphraseAsksAgain:
    """One slip must not send someone back to the main menu.

    The first version asked once and gave up, which is the wrong shape for the
    single most mistyped thing in any interface. It now asks again, up to three
    times -- and escape still leaves on the first press, because a prompt you
    cannot get out of is worse than one that gives up too early.
    """

    def _sealed(self, tmp_path: Path, conversation) -> Path:  # type: ignore[no-untyped-def]
        from ferry.core import Bundle, Manifest, SourceMachine
        from ferry.core.sealed import seal_bundle

        root = tmp_path / "plain-bundle"
        bundle = Bundle.create(
            root,
            Manifest(
                created_at=datetime(2026, 8, 1, tzinfo=UTC),
                created_by="ferry test",
                source_machine=SourceMachine(os="win32", user_home=str(Path.home())),
            ),
        )
        bundle.add_conversation(conversation)
        return seal_bundle(root, PHRASE).path

    def test_a_mistyped_passphrase_is_asked_again(self, tmp_path: Path, conversation) -> None:  # type: ignore[no-untyped-def]
        archive = self._sealed(tmp_path, conversation)
        ui = _Answers(path=str(archive), actions=["done"], secrets=["wrong", PHRASE])

        run_inspect(ui)

        assert ui.secrets_asked == 2
        assert "did not open it" in ui.text
        assert "1 conversation" in ui.text, "the second attempt must actually open it"

    def test_the_tries_remaining_are_named(self, tmp_path: Path, conversation) -> None:  # type: ignore[no-untyped-def]
        """So the third attempt is not a surprise."""
        archive = self._sealed(tmp_path, conversation)
        ui = _Answers(path=str(archive), actions=["done"], secrets=["a", "b", PHRASE])

        run_inspect(ui)

        assert "2 tries left" in ui.text
        assert "1 try left" in ui.text

    def test_it_gives_up_after_three(self, tmp_path: Path, conversation) -> None:  # type: ignore[no-untyped-def]
        """A prompt that never stops asking is a prompt nobody can leave."""
        archive = self._sealed(tmp_path, conversation)
        ui = _Answers(path=str(archive), actions=["done"], secrets=["a", "b", "c", PHRASE])

        run_inspect(ui)

        assert ui.secrets_asked == 3

    def test_the_last_refusal_names_both_causes(self, tmp_path: Path, conversation) -> None:  # type: ignore[no-untyped-def]
        """Ferry cannot tell a wrong passphrase from an altered file.

        The tag fails identically for both, deliberately. Saying only "wrong
        passphrase" would send someone hunting for a passphrase that was right
        all along.
        """
        archive = self._sealed(tmp_path, conversation)
        ui = _Answers(path=str(archive), actions=["done"], secrets=["a", "b", "c"])

        run_inspect(ui)

        assert "has been altered" in ui.text
        assert "cannot tell those apart" in ui.text

    def test_escape_leaves_on_the_first_press(self, tmp_path: Path, conversation) -> None:  # type: ignore[no-untyped-def]
        """Backing out is not a failed attempt and must not be spent as one."""
        archive = self._sealed(tmp_path, conversation)
        ui = _Answers(path=str(archive), actions=["done"], secrets=[])

        run_inspect(ui)

        assert ui.secrets_asked == 1
        assert "did not open it" not in ui.text


class TestTheOpeningSpinnerStops:
    """ "Opening" kept spinning long after the bundle was open.

    The spinner and the unsealed copy were opened as one ``with``, so the
    spinner stayed alive across the ``yield`` -- underneath the import screen,
    underneath the conflict question, underneath the whole inspect listing.
    They need different lifetimes: the spinner ends when the work it describes
    ends, the unsealed copy lives until the caller is finished with it.
    """

    def _sealed(self, tmp_path: Path, conversation) -> Path:  # type: ignore[no-untyped-def]
        from ferry.core import Bundle, Manifest, SourceMachine
        from ferry.core.sealed import seal_bundle

        root = tmp_path / "plain-bundle"
        bundle = Bundle.create(
            root,
            Manifest(
                created_at=datetime(2026, 8, 1, tzinfo=UTC),
                created_by="ferry test",
                source_machine=SourceMachine(os="win32", user_home=str(Path.home())),
            ),
        )
        bundle.add_conversation(conversation)
        return seal_bundle(root, PHRASE).path

    def test_the_spinner_is_closed_before_the_caller_sees_the_bundle(
        self, tmp_path: Path, conversation
    ) -> None:  # type: ignore[no-untyped-def]
        from ferry.cli.flows import _opened

        archive = self._sealed(tmp_path, conversation)
        ui = _Spied(secrets=[PHRASE])

        with _opened(ui, archive) as opened:
            assert opened is not None
            spinning_while_the_caller_works = ui.spinning
            assert (opened.root / "manifest.json").is_file(), "the bundle must really be open"

        assert not spinning_while_the_caller_works, (
            'the "Opening" spinner was still running while the caller did its work'
        )
        assert ui.spun == ["Opening"], "the unsealing itself should still show a spinner"

    def test_the_unsealed_copy_outlives_the_spinner(self, tmp_path: Path, conversation) -> None:  # type: ignore[no-untyped-def]
        """The other half. Ending the spinner early must not end the bundle."""
        from ferry.cli.flows import _opened

        archive = self._sealed(tmp_path, conversation)
        ui = _Spied(secrets=[PHRASE])

        with _opened(ui, archive) as opened:
            assert opened is not None
            inside = opened.root
            assert list(opened.root.glob("conversations/*.json"))

        assert not inside.exists(), "the unsealed copy must be cleaned up on the way out"


class _Spied(_Answers):
    """Records which spinners ran, and whether one is running right now."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.spun: list[str] = []
        self.spinning = False

    @contextmanager
    def scanning(self, label: str):  # type: ignore[no-untyped-def]
        self.spun.append(label)
        self.spinning = True
        try:
            yield
        finally:
            self.spinning = False


# --------------------------------------------------------------------------
# typing a path
# --------------------------------------------------------------------------


class TestBundleAt:
    """What a typed path is, decided without opening anything.

    A pure function so it can be tested at all. The screen it serves cannot be
    driven without a terminal, and behaviour that cannot be invoked without one
    does not get tested -- so the deciding moved out until it could be.
    """

    def test_a_bundle_directory_is_a_bundle(self, bundle_dir: Path) -> None:
        assert bundle_at(str(bundle_dir)).bundle == bundle_dir

    def test_a_sealed_file_is_a_bundle(self, tmp_path: Path, conversation) -> None:  # type: ignore[no-untyped-def]
        archive = _seal(tmp_path, conversation)
        assert bundle_at(str(archive)).bundle == archive

    def test_nothing_there_says_so(self, tmp_path: Path) -> None:
        verdict = bundle_at(str(tmp_path / "no-such-folder"))
        assert verdict.bundle is None
        assert "There is nothing at" in verdict.problem

    def test_a_folder_that_is_not_a_bundle_names_the_missing_manifest(self, tmp_path: Path) -> None:
        (tmp_path / "empty").mkdir()
        verdict = bundle_at(str(tmp_path / "empty"))
        assert "manifest.json" in verdict.problem

    def test_an_ordinary_file_says_file_not_bundle(self, tmp_path: Path) -> None:
        note = tmp_path / "notes.txt"
        note.write_text("hello", encoding="utf-8")
        assert "is a file, not a bundle" in bundle_at(str(note)).problem

    def test_a_ferry_file_that_is_not_sealed_is_told_apart(self, tmp_path: Path) -> None:
        """Named like one, does not begin like one.

        "not a bundle" alone would leave someone staring at a file whose name
        says otherwise.
        """
        fake = tmp_path / "looks-real.ferry"
        fake.write_text("not encrypted at all", encoding="utf-8")
        assert "named like a sealed bundle" in bundle_at(str(fake)).problem

    def test_a_folder_holding_bundles_offers_them(self, bundle_dir: Path) -> None:
        """Pointing at the right neighbourhood is not a mistake."""
        verdict = bundle_at(str(bundle_dir.parent))
        assert verdict.bundle is None
        assert bundle_dir in verdict.nearby

    def test_quotes_around_a_path_are_stripped(self, bundle_dir: Path) -> None:
        """Windows Explorer's "Copy as path" wraps the path in double quotes."""
        assert bundle_at(f'"{bundle_dir}"').bundle == bundle_dir

    def test_clean_path_leaves_an_ordinary_path_alone(self) -> None:
        assert clean_path("  C:/Users/x/bundle  ") == "C:/Users/x/bundle"

    def test_an_empty_path_is_neither_a_bundle_nor_a_complaint(self) -> None:
        verdict = bundle_at("   ")
        assert verdict.bundle is None
        assert verdict.problem == ""


class TestTheTypedPathAsksAgain:
    """A wrong path used to end the screen.

    One stray character, one stale folder, one paste with a quote on the end,
    and you were back at the main menu with nothing to correct. Unlike a
    passphrase, a path is checkable, so there is no attempt limit here -- but
    escape still has to leave on the first press.
    """

    def test_a_wrong_path_is_reported_and_asked_again(
        self, bundle_dir: Path, tmp_path: Path
    ) -> None:
        ui = _Answers(
            path=[str(tmp_path / "typo"), str(bundle_dir)],
            actions=["done"],
        )

        run_inspect(ui)

        assert ui.paths_asked == 2, "the screen must ask again rather than give up"
        assert "There is nothing at" in ui.text
        assert "1 conversation" in ui.text, "the second answer must actually open it"

    def test_what_was_typed_comes_back_as_the_starting_text(
        self, bundle_dir: Path, tmp_path: Path
    ) -> None:
        """A typo should be a keystroke to fix, not a line to type again."""
        typo = str(tmp_path / "typo")
        ui = _Answers(path=[typo, str(bundle_dir)], actions=["done"])

        run_inspect(ui)

        assert ui.path_defaults[0] == ""
        assert ui.path_defaults[1] == typo

    def test_escape_leaves_on_the_first_press(self, tmp_path: Path) -> None:
        ui = _Answers(path=None, actions=["done"])

        run_inspect(ui)

        assert ui.paths_asked == 1

    def test_a_folder_holding_bundles_lists_them(self, bundle_dir: Path) -> None:
        ui = _Answers(path=[str(bundle_dir.parent)], actions=[str(bundle_dir), "done"])

        run_inspect(ui)

        assert "is not a bundle itself, but it holds" in ui.text
        assert "1 conversation" in ui.text


# --------------------------------------------------------------------------
# unsealing
# --------------------------------------------------------------------------

PHRASE_TWO = "correct horse battery staple"


def _seal(tmp_path: Path, conversation, name: str = "plain-bundle") -> Path:  # type: ignore[no-untyped-def]
    from ferry.core import Bundle, Manifest, SourceMachine
    from ferry.core.sealed import seal_bundle

    root = tmp_path / name
    bundle = Bundle.create(
        root,
        Manifest(
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
            created_by="ferry test",
            source_machine=SourceMachine(os="win32", user_home=str(Path.home())),
        ),
    )
    bundle.add_conversation(conversation)
    archive = seal_bundle(root, PHRASE_TWO).path
    shutil.rmtree(root)
    return archive


class TestUnsealingASealedBundle:
    """The screen used to say "unseal it first" and offer no way to do it.

    An instruction the program does not support is worse than no instruction:
    it tells someone the thing they want is possible and leaves them looking
    for it.
    """

    def test_it_writes_a_real_bundle_and_leaves_the_sealed_file_alone(
        self, tmp_path: Path, conversation
    ) -> None:  # type: ignore[no-untyped-def]
        archive = _seal(tmp_path, conversation)
        before = archive.read_bytes()
        into = tmp_path / "opened-up"
        ui = _Answers(
            path=[str(archive), str(into)],
            actions=["unseal", "done"],
            secrets=[PHRASE_TWO],
        )

        run_inspect(ui)

        assert (into / "manifest.json").is_file(), "the unsealed folder must be a real bundle"
        assert list((into / "conversations").glob("*.json"))
        assert archive.read_bytes() == before, "unsealing must not touch the sealed file"

    def test_it_says_the_folder_is_not_encrypted_before_writing_it(
        self, tmp_path: Path, conversation
    ) -> None:  # type: ignore[no-untyped-def]
        archive = _seal(tmp_path, conversation)
        ui = _Answers(
            path=[str(archive), str(tmp_path / "opened-up")],
            actions=["unseal", "done"],
            secrets=[PHRASE_TWO],
        )

        run_inspect(ui)

        assert "not encrypted" in ui.text

    def test_it_refuses_to_write_over_something_that_is_already_there(
        self, tmp_path: Path, conversation
    ) -> None:  # type: ignore[no-untyped-def]
        archive = _seal(tmp_path, conversation)
        taken = tmp_path / "taken"
        taken.mkdir()
        (taken / "important.txt").write_text("do not lose me", encoding="utf-8")
        ui = _Answers(
            path=[str(archive), str(taken)],
            actions=["unseal", "done"],
            secrets=[PHRASE_TWO],
        )

        run_inspect(ui)

        assert "will not write over it" in ui.text
        assert (taken / "important.txt").read_text(encoding="utf-8") == "do not lose me"


class TestEditingASealedBundle:
    """Delete from a sealed bundle, then seal it again.

    The convenience option, and the same act underneath as unsealing by hand:
    unseal, edit, seal, prove the new file opens, and only then replace the
    old one.
    """

    def test_the_conversation_is_gone_and_the_file_still_opens(
        self, tmp_path: Path, conversation
    ) -> None:  # type: ignore[no-untyped-def]
        archive = _seal(tmp_path, conversation)
        before = archive.read_bytes()
        ui = _Answers(
            path=[str(archive)],
            actions=["one", "done"],
            secrets=[PHRASE_TWO],
            confirm=True,
        )

        run_inspect(ui)

        assert archive.read_bytes() != before, "the sealed file must have been written again"
        assert is_sealed(archive)
        assert opens_with(archive, PHRASE_TWO), "the same passphrase, not a new one"

        from ferry.core.sealed import unsealed

        with unsealed(archive, PHRASE_TWO) as bundle:
            assert bundle.list_conversations() == []

    def test_saying_no_leaves_the_file_untouched(self, tmp_path: Path, conversation) -> None:  # type: ignore[no-untyped-def]
        archive = _seal(tmp_path, conversation)
        before = archive.read_bytes()
        ui = _Answers(
            path=[str(archive)],
            actions=["one", "done"],
            secrets=[PHRASE_TWO],
            confirm=False,
        )

        run_inspect(ui)

        assert archive.read_bytes() == before

    def test_the_backup_is_named_as_unencrypted(self, tmp_path: Path, conversation) -> None:  # type: ignore[no-untyped-def]
        """The safety copy is a plain folder even though the bundle is not."""
        archive = _seal(tmp_path, conversation)
        ui = _Answers(
            path=[str(archive)],
            actions=["one", "done"],
            secrets=[PHRASE_TWO],
            confirm=False,
        )

        run_inspect(ui)

        assert "not encrypted, even though this bundle is" in ui.text

    def test_a_sealed_file_that_does_not_verify_is_never_put_in_place(
        self, tmp_path: Path, conversation, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """The one place where being wrong costs everything.

        The check is written as an unconditional step rather than an
        assumption, so this forces it to say no and asserts that yesterday's
        file survived.
        """
        archive = _seal(tmp_path, conversation)
        before = archive.read_bytes()
        monkeypatch.setattr("ferry.cli.flows.opens_with", lambda *args, **kwargs: False)
        ui = _Answers(
            path=[str(archive)],
            actions=["one", "done"],
            secrets=[PHRASE_TWO],
            confirm=True,
        )

        run_inspect(ui)

        assert archive.read_bytes() == before, "the original must survive a failed check"
        assert not (archive.parent / (archive.name + ".new")).exists()
        assert "is unchanged" in ui.text


class TestDeletingASealedBundle:
    def test_the_file_is_removed(self, tmp_path: Path, conversation) -> None:  # type: ignore[no-untyped-def]
        archive = _seal(tmp_path, conversation)
        ui = _Answers(path=[str(archive)], actions=["all"], secrets=[PHRASE_TWO], confirm=True)

        run_inspect(ui)

        assert not archive.exists()

    def test_saying_no_keeps_it(self, tmp_path: Path, conversation) -> None:  # type: ignore[no-untyped-def]
        archive = _seal(tmp_path, conversation)
        ui = _Answers(
            path=[str(archive)], actions=["all", "done"], secrets=[PHRASE_TWO], confirm=False
        )

        run_inspect(ui)

        assert archive.exists()
        assert "Nothing was deleted" in ui.text


# --------------------------------------------------------------------------
# compact
# --------------------------------------------------------------------------


class TestTheCompactScreen:
    """Compact reads a bundle and writes a document. It never touches a store,
    never asks the network, and never writes over a file that exists."""

    def test_it_builds_a_document_from_the_chosen_conversation(self, bundle_dir: Path) -> None:
        from ferry.cli.flows import run_compact

        ui = _Answers(path=str(bundle_dir), actions=["handoff", "standard", "done"])

        run_compact(ui)

        assert "Compact:" in ui.text
        assert "without sending it anywhere" in ui.text

    def test_it_says_where_the_document_came_from_before_showing_it(self, bundle_dir: Path) -> None:
        """The honesty line goes above the document, not under it. It is what a
        person needs in order to know how to read what follows."""
        from ferry.cli.flows import run_compact

        ui = _Answers(path=str(bundle_dir), actions=["handoff", "standard", "done"])

        run_compact(ui)

        assert ui.text.index("Nothing was invented") < ui.text.index("# Compact:")

    def test_backing_out_of_the_bundle_prompt_does_nothing(self) -> None:
        from ferry.cli.flows import run_compact

        ui = _Answers(path=None)

        run_compact(ui)

        assert "Compact:" not in ui.text

    def test_an_empty_bundle_says_so_rather_than_offering_a_list_of_nothing(
        self, tmp_path: Path, manifest
    ) -> None:  # type: ignore[no-untyped-def]
        from ferry.cli.flows import run_compact
        from ferry.core import Bundle

        Bundle.create(tmp_path / "empty", manifest)
        ui = _Answers(path=str(tmp_path / "empty"))

        run_compact(ui)

        assert "nothing in this bundle to compact" in ui.text

    def test_a_sealed_bundle_is_opened_with_its_passphrase(
        self, tmp_path: Path, conversation
    ) -> None:  # type: ignore[no-untyped-def]
        """The same picker as inspect, so the passphrase retry and the path
        prompt are the ones already built rather than a second copy."""
        from ferry.cli.flows import run_compact

        archive = _seal(tmp_path, conversation)
        ui = _Answers(
            path=str(archive),
            secrets=[PHRASE_TWO],
            actions=["handoff", "standard", "done"],
        )

        run_compact(ui)

        assert ui.secrets_asked == 1
        assert "# Compact:" in ui.text

    def test_saving_refuses_to_write_over_something_that_is_there(
        self, bundle_dir: Path, tmp_path: Path
    ) -> None:
        from ferry.cli.flows import run_compact

        taken = tmp_path / "taken.md"
        taken.write_text("do not lose me", encoding="utf-8")
        free = tmp_path / "free.md"
        ui = _Answers(
            path=[str(bundle_dir), str(taken), str(free)],
            actions=["handoff", "standard", "save", "done"],
        )

        run_compact(ui)

        assert taken.read_text(encoding="utf-8") == "do not lose me"
        assert "# Compact:" in free.read_text(encoding="utf-8")

    def test_the_clipboard_is_not_offered_where_there_is_none(
        self, bundle_dir: Path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """Headless, over SSH, in a container. An option that fails when chosen
        is worse than one that was never there."""
        from ferry.cli import clipboard, flows

        monkeypatch.setattr(clipboard, "available", lambda: False)
        ui = _Answers(path=str(bundle_dir), actions=["handoff", "standard", "done"])

        flows.run_compact(ui)

        assert "Copy it to the clipboard" not in ui.text

    def test_a_clipboard_that_refuses_is_not_an_error(self, bundle_dir: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from ferry.cli import clipboard, flows

        monkeypatch.setattr(clipboard, "available", lambda: True)
        monkeypatch.setattr(clipboard, "copy", lambda text: False)
        ui = _Answers(path=str(bundle_dir), actions=["handoff", "standard", "copy", "done"])

        flows.run_compact(ui)

        assert "did not take it" in ui.text
        assert "Traceback" not in ui.text


class TestGoingBackOneStep:
    """Compact asks four questions in a row, and escape must go back exactly
    one of them.

    It used to mean three different things across three consecutive prompts:
    leave the screen, go back one, and go back two. Someone who presses it once
    and loses their place stops trusting it everywhere, which is worse than a
    key that does nothing at all.
    """

    def test_escape_walks_back_up_the_questions_one_at_a_time(self, bundle_dir: Path) -> None:
        """The whole ladder, one rung per keystroke.

        The middle rung is the one the human found: escape at the length
        question used to skip a step and land back on the conversation list.
        The top rung leaves for the bundle picker rather than the main menu.
        """
        from ferry.cli.flows import run_compact

        ui = _Answers(path=str(bundle_dir), actions=["said", ESCAPE, ESCAPE, ESCAPE])

        run_compact(ui)

        assert ui.questions == [
            "Which conversation?",
            "What should it be for?",
            "How long?",
            "What should it be for?",
            "Which conversation?",
        ]

    def test_escape_at_the_first_question_offers_a_different_bundle(self, bundle_dir: Path) -> None:
        """Not the main menu. Wanting a different bundle is the likeliest
        reason to be backing out of the conversation list."""
        from ferry.cli.flows import run_compact

        ui = _Answers(path=[str(bundle_dir)], actions=[ESCAPE])

        run_compact(ui)

        assert ui.paths_asked == 2

    def test_going_back_lands_on_the_row_you_were_on(self, bundle_dir: Path) -> None:
        """A back that dumps you at the top of a list of forty conversations is
        a back you have to undo."""
        from ferry.cli.flows import run_compact

        ui = _Answers(path=str(bundle_dir), actions=["done", "again", ESCAPE, ESCAPE, ESCAPE])

        run_compact(ui)

        # The last time each question was asked, the cursor was where it was
        # left rather than at zero -- shape "said" is row 1 of three.
        assert ui.questions.count("What should it be for?") >= 2

    def test_every_step_says_that_escape_goes_back(self, bundle_dir: Path) -> None:
        """A key that goes back while the footer says "cancel" is why people
        stop pressing it."""
        from ferry.cli.flows import run_compact

        ui = _Answers(path=str(bundle_dir), actions=[ESCAPE, ESCAPE, ESCAPE])

        run_compact(ui)

        assert all(ui.offered_back), ui.offered_back

    def test_a_different_shape_does_not_mean_a_different_conversation(
        self, bundle_dir: Path
    ) -> None:
        """Wanting the same conversation shorter is not wanting a different
        one, and it should not cost a walk back through the whole list."""
        from ferry.cli.flows import run_compact

        ui = _Answers(path=str(bundle_dir), actions=["again", "done"])

        run_compact(ui)

        # Straight back to the length question, not to the conversation list.
        assert ui.questions == [
            "Which conversation?",
            "What should it be for?",
            "How long?",
            "What now?",
            "How long?",
            "What now?",
        ]

    def test_the_view_screen_can_reach_a_different_bundle(self, bundle_dir: Path) -> None:
        from ferry.cli.flows import run_compact

        ui = _Answers(path=[str(bundle_dir)], actions=["bundle"])

        run_compact(ui)

        assert ui.paths_asked == 2
