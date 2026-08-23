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
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest

from ferry.adapters.base import (
    Adapter,
    DetectResult,
    ExportEvent,
    ImportEvent,
    ImportOptions,
)
from ferry.cli.flows import _describe, find_bundles, run_export, run_import
from ferry.cli.theme import MONO, Capability
from ferry.cli.ui import UI, NonInteractiveError


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

    def detect(self) -> DetectResult:
        return DetectResult(installed=self._installed, conversation_count_estimate=2)

    def export(self, dest_bundle_dir: Path) -> Iterator[ExportEvent]:
        self.exported_to = dest_bundle_dir
        yield from self._events  # type: ignore[misc]

    def import_(self, bundle_dir: Path, options: ImportOptions) -> Iterator[ImportEvent]:
        self.imported_from = bundle_dir
        yield from self._events  # type: ignore[misc]


class _Answers(UI):
    """A UI with the prompts pre-answered."""

    def __init__(self, *, path: str | None = None, confirm: bool | None = True) -> None:
        super().__init__(MONO, capability=Capability.PLAIN)
        # The real console, redirected -- not a replacement. A bare rich Console
        # has none of the ferry.* styles, so swapping it out tests a renderer
        # the user never sees and hides any style name that does not exist.
        self.buffer = io.StringIO()
        self.console.file = self.buffer
        self.console.width = 100
        self._path = path
        self._confirm = confirm
        self.asked_to_confirm = 0

    def select(self, question, choices, **kwargs):  # type: ignore[no-untyped-def]
        return choices[0][0]

    def path(self, question, *, default="", hint=""):  # type: ignore[no-untyped-def]
        return self._path

    def confirm(self, question, *, default, hint=""):  # type: ignore[no-untyped-def]
        self.asked_to_confirm += 1
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
    ui = _Answers(path=str(bundle_dir), confirm=False)

    run_import(ui, scanned(adapter))

    assert adapter.imported_from is None
    assert "Nothing was written" in ui.text


def test_import_says_it_is_writing_into_real_history(bundle_dir: Path) -> None:
    adapter = _Recorder()
    ui = _Answers(path=str(bundle_dir), confirm=False)

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
    ui = _Watched(path=str(bundle_dir), confirm=True)

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
