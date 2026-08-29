"""The presentation layer. Everything a user sees goes through here.

No milestone after M2 may print with ``rich`` or prompt with ``questionary``
directly (PROGRESS.md ledger #64). Routing it all through one module is what
keeps theming consistent — the moment an adapter prints its own coloured output,
the theme silently stops applying to half the interface.

The other job of this module is degrading safely. Every prompt here refuses to
run without a TTY rather than blocking forever on input that can never arrive,
and every styled write goes through a console that knows whether colour is
available.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol

import questionary
from questionary import Choice
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    Task,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text
from rich.theme import Theme as RichTheme

from ferry.cli.motion import FRAME_SECONDS, Motion, moored_frame, spinner_frames
from ferry.cli.theme import (
    ASCII_ICONS,
    DEFAULT_THEME,
    Capability,
    Theme,
    detect_capability,
    resolve_theme,
)

__all__ = ["UI", "NonInteractiveError", "ProgressHandle"]


class ProgressHandle(Protocol):
    """What :meth:`UI.progress` hands back.

    Stated as a protocol so callers are type-checked against it. The bar and
    the piped fallback are different classes with nothing in common but this,
    and a caller that only knows ``object`` has to reach past the type checker
    to use either.
    """

    def advance(self, n: int = 1) -> None:
        """Record ``n`` more units of work done."""

    def describe(self, text: str) -> None:
        """Say what is being worked on now."""


class NonInteractiveError(RuntimeError):
    """Raised when a prompt is attempted without a usable terminal.

    Carries the flag-based equivalent so the caller can tell the user exactly
    what to run instead of leaving them stuck.
    """

    def __init__(self, action: str, hint: str) -> None:
        self.action = action
        self.hint = hint
        super().__init__(f"{action} needs an interactive terminal. {hint}")


class _WakeColumn(ProgressColumn):
    """The progress-bar spinner: the same ferry, sailing while work happens.

    ``rich``'s own ``SpinnerColumn`` can only take a spinner registered in its
    global table, so the hull is drawn here instead. On completion the wake
    settles and the hull turns the success colour — the bar finishing and the
    ferry arriving are the same event.
    """

    max_refresh = FRAME_SECONDS

    def __init__(self, *, ascii_only: bool) -> None:
        self.frames = spinner_frames(width=3, ascii_only=ascii_only)
        self.moored = moored_frame(3, ascii_only=ascii_only)
        super().__init__()

    def render(self, task: Task) -> Text:
        if task.finished:
            return Text(self.moored, style="ferry.success")
        tick = int(time.monotonic() / FRAME_SECONDS)
        return Text(self.frames[tick % len(self.frames)], style="ferry.primary")


class _SailingSpinner:
    """A ``rich`` renderable that draws the ferry sailing, based on the clock.

    Written as a time-driven renderable rather than a frame list handed to
    ``rich``'s spinner registry: ``Live`` re-renders on its own schedule, so
    reading the clock here means no background thread and no mutation of
    ``rich``'s module-level ``SPINNERS`` dictionary.
    """

    def __init__(self, label: str, *, ascii_only: bool) -> None:
        self.label = label
        self.frames = spinner_frames(ascii_only=ascii_only)
        self._started = time.monotonic()

    def __rich_console__(self, console: object, options: object) -> Iterator[Text]:
        tick = int((time.monotonic() - self._started) / FRAME_SECONDS)
        out = Text("  ")
        out.append(self.frames[tick % len(self.frames)], style="ferry.primary")
        out.append("  ")
        out.append(self.label, style="ferry.dim")
        yield out


@dataclass
class _DetectionRow:
    """One tool's line on the scan screen."""

    display_name: str
    installed: bool
    detail: str


class UI:
    """Themed console output and prompts.

    Args:
        theme: Theme to use. Resolved from flags/env when ``None``.
        capability: Detected terminal capability. Detected when ``None``.
        verbose: Whether to emit debug-level detail.
    """

    def __init__(
        self,
        theme: Theme | None = None,
        *,
        capability: Capability | None = None,
        verbose: bool = False,
    ) -> None:
        self.capability = detect_capability() if capability is None else capability
        self.theme = resolve_theme(capability=self.capability) if theme is None else theme
        self.verbose = verbose
        self.console = self._build_console()

    def _build_console(self) -> Console:
        """Build a ``rich`` console wired to the active theme."""
        t = self.theme
        if not t.uses_color:
            return Console(no_color=True, highlight=False, soft_wrap=True)
        return Console(
            theme=RichTheme(
                {
                    "ferry.primary": t.primary,
                    "ferry.accent": t.accent,
                    "ferry.success": t.success,
                    "ferry.error": t.error,
                    "ferry.warning": t.warning,
                    "ferry.dim": t.dim,
                    "ferry.text": t.text,
                    "ferry.heading": t.heading,
                }
            ),
            highlight=False,
        )

    def set_theme(self, name: str) -> None:
        """Switch theme mid-session and rebuild the console.

        Goes through :func:`resolve_theme` rather than looking the name up
        directly, so a theme picked interactively is still subject to the same
        capability downgrades as one passed with ``--theme``.
        """
        self.theme = resolve_theme(name, capability=self.capability)
        self.console = self._build_console()

    @property
    def interactive(self) -> bool:
        """Whether prompts can actually be shown."""
        return self.capability is not Capability.PLAIN

    def _style(self, token: str, text: str) -> str:
        """Wrap ``text`` in a theme style, or leave it bare under ``mono``."""
        if not self.theme.uses_color:
            return text
        return f"[{token}]{text}[/{token}]"

    # ---------- output ----------

    def banner(self, *, animate: bool = True) -> None:
        """Print the animated wordmark.

        Imported here rather than at module level because ``brand`` needs the
        ``UI`` type for its own annotations.
        """
        from ferry.cli import brand

        brand.render(self, self.theme, animate=animate)

    def info(self, message: str) -> None:
        """Print a neutral status line."""
        icon = self.theme.icons.info
        self.console.print(f"  {self._style('ferry.dim', icon)} {message}")

    def success(self, message: str) -> None:
        """Print a success line."""
        icon = self.theme.icons.success
        self.console.print(f"  {self._style('ferry.success', icon)} {message}")

    def warn(self, message: str) -> None:
        """Print a warning line."""
        icon = self.theme.icons.warning
        self.console.print(f"  {self._style('ferry.warning', icon)} {message}")

    def error(self, message: str) -> None:
        """Print an error line."""
        icon = self.theme.icons.error
        self.console.print(f"  {self._style('ferry.error', icon)} {message}")

    def detail(self, message: str) -> None:
        """Print one indented line of a running list.

        For the per-conversation lines an export or import produces: quieter
        than :meth:`info` and indented under it, because a hundred of them
        should read as one block of progress rather than a hundred
        announcements.
        """
        icon = self.theme.icons.success
        self.console.print(f"    {self._style('ferry.dim', icon + ' ' + message)}")

    def debug(self, message: str) -> None:
        """Print detail, but only under ``--verbose``."""
        if self.verbose:
            self.console.print(f"  {self._style('ferry.dim', message)}")

    def blank(self) -> None:
        """Print a blank line."""
        self.console.print()

    def detection_table(self, rows: Iterable[_DetectionRow]) -> None:
        """Render the scan results.

        A plain grid rather than a bordered table — the scan screen is the first
        thing a user sees, and boxes around four short lines read as clutter.
        """
        icons = self.theme.icons
        table = Table.grid(padding=(0, 2))
        table.add_column(width=max(len(icons.success), len(icons.absent)))
        table.add_column()
        # `fold` rather than rich's default: a detail line can carry a long
        # absolute path, and ellipsising it substitutes a Unicode `…` that the
        # rest of the theme has gone to some trouble to avoid on consoles that
        # cannot encode it.
        table.add_column(overflow="fold")
        for row in rows:
            if row.installed:
                mark = self._style("ferry.success", icons.success)
                name = self._style("ferry.text", row.display_name)
                detail = self._style("ferry.accent", row.detail)
            else:
                # `absent`, not `error` — a tool you simply do not have
                # installed is information, not a failure, and an error cross
                # against four rows reads as though something went wrong.
                mark = self._style("ferry.dim", icons.absent)
                name = self._style("ferry.dim", row.display_name)
                detail = self._style("ferry.dim", row.detail)
            table.add_row(mark, name, detail)
        self.console.print(table)

    @contextmanager
    def scanning(self, label: str) -> Iterator[None]:
        """Show a spinner while work happens, if the terminal can show one.

        Falls back to a static line under ``mono`` or when piped, so nothing
        writes escape codes into a log.
        """
        if not self.theme.uses_color or not self.interactive:
            self.console.print(f"  {label}")
            yield
            return
        from rich.live import Live

        spinner = _SailingSpinner(label, ascii_only=self.theme.icons is ASCII_ICONS)
        with Live(spinner, console=self.console, refresh_per_second=12, transient=True):
            yield

    @contextmanager
    def progress(self, label: str, total: int) -> Iterator[ProgressHandle]:
        """Show a progress bar for a roughly known amount of work.

        Yields an object with ``advance(n=1)`` and ``describe(text)``. The
        total may be an estimate: going past it raises the total rather than
        letting the bar sit full while work continues.

        Under ``mono`` or when piped, the bar is suppressed and only start and
        finish lines are printed -- no escape codes ever reach a log.
        """
        if not self.theme.uses_color or not self.interactive:
            self.console.print(f"  {label} (0/{total})")

            class _Silent:
                def __init__(self) -> None:
                    self.done = 0

                def advance(self, n: int = 1) -> None:
                    self.done += n

                def describe(self, text: str) -> None:
                    """No-op. A label that changes 200 times is noise in a log."""

            silent = _Silent()
            yield silent
            self.console.print(f"  {label} ({silent.done}/{total})")
            return

        icons = self.theme.icons
        with Progress(
            _WakeColumn(ascii_only=icons is ASCII_ICONS),
            TextColumn("[ferry.text]{task.description}"),
            BarColumn(
                bar_width=24,
                complete_style="ferry.primary",
                finished_style="ferry.success",
                pulse_style="ferry.dim",
            ),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=self.console,
            transient=False,
        ) as prog:
            task = prog.add_task(label, total=total)

            class _Live:
                def __init__(self) -> None:
                    self.done = 0
                    self.total = total

                def advance(self, n: int = 1) -> None:
                    self.done += n
                    # Callers pass an estimate. A bar pinned at 100% while work
                    # visibly continues is worse than one that grows, so the
                    # total follows reality rather than the guess.
                    if self.done > self.total:
                        self.total = self.done
                        prog.update(task, total=self.total)
                    prog.advance(task, n)

                def describe(self, text: str) -> None:
                    """Change the label to name what is being worked on now."""
                    prog.update(task, description=text)

            yield _Live()

    # ---------- prompts ----------

    def _require_interactive(self, action: str, hint: str) -> None:
        """Refuse to prompt when there is no terminal to prompt on."""
        if not self.interactive:
            raise NonInteractiveError(action, hint)

    def select(
        self,
        question: str,
        choices: Sequence[tuple[str, ...]],
        *,
        hint: str = "",
        allow_filter: bool = True,
        motions: Mapping[str, Motion] | None = None,
        current: str | None = None,
        initial: int = 0,
        back: bool = False,
    ) -> str | None:
        """Ask the user to pick one option.

        Args:
            question: The prompt text.
            choices: ``(value, label)`` pairs, or ``(value, label, note)``
                triples where the note is a sentence shown on its own line
                under the label. Mixed freely in one list.
            hint: Flag-based equivalent, shown if there is no terminal.
            allow_filter: Whether ``/`` opens the filter.
            motions: Optional animated icon per choice value. Passed in
                explicitly rather than looked up by value, so a list whose
                values happen to collide with action names cannot pick up
                icons it never asked for.
            current: Value already in force, marked "in use" in the list. Pass
                it from every picker that changes a persistent setting.
            initial: Which row the cursor starts on. Pass it when returning to
                a question the user has already answered, so going back lands
                them where they were rather than at the top.
            back: Whether escape goes back a step rather than leaving. Only
                changes what the footer promises; the return is ``None`` either
                way.

        Returns:
            The chosen value, or ``None`` if the user cancelled or went back.
        """
        self._require_interactive(question, hint)
        from ferry.cli.prompts import SelectorItem, run_select

        marks = motions or {}
        return run_select(
            question,
            [
                SelectorItem(
                    row[0], row[1], note=row[2] if len(row) > 2 else "", motion=marks.get(row[0])
                )
                for row in choices
            ],
            theme=self.theme,
            allow_filter=allow_filter,
            current=current,
            initial=initial,
            back=back,
        )

    def multiselect(
        self,
        question: str,
        choices: Sequence[tuple[str, str]],
        *,
        hint: str = "",
        preselected: Sequence[str] | None = None,
    ) -> list[str] | None:
        """Ask the user to tick any number of options.

        Everything is ticked by default unless ``preselected`` says otherwise —
        the common case is "take all of it".
        """
        self._require_interactive(question, hint)
        chosen = set(preselected) if preselected is not None else {v for v, _ in choices}
        answer = questionary.checkbox(
            question,
            choices=[
                Choice(title=label, value=value, checked=value in chosen)
                for value, label in choices
            ],
            qmark=self.theme.icons.info,
            pointer=self.theme.icons.cursor,
        ).ask()
        return answer if isinstance(answer, list) else None

    def confirm(self, question: str, *, default: bool, hint: str = "") -> bool | None:
        """Ask a yes/no question.

        Rendered as an arrow-selectable Yes/No, never as a typed ``[y/N]``
        prompt (PROGRESS.md ledger #71).

        ``default`` is deliberately a required argument: read operations should
        default to yes, anything that writes to a user's real data should
        default to no. It decides which option starts under the cursor.
        """
        self._require_interactive(question, hint)
        from ferry.cli.prompts import run_confirm

        return run_confirm(question, theme=self.theme, default=default)

    def secret(self, question: str, *, hint: str = "") -> str | None:
        """Ask for a passphrase. Nothing is echoed, and nothing is remembered.

        Ferry never stores a bundle passphrase, never puts one in a log or an
        error message, and has nowhere to recover one from. Returns ``None``
        when the user backs out or types nothing.
        """
        self._require_interactive(question, hint)
        from ferry.cli.prompts import run_secret

        return run_secret(question, theme=self.theme)

    def path(self, question: str, *, default: str = "", hint: str = "") -> str | None:
        """Ask for a filesystem path, with tab completion.

        Returns ``None`` when the user backs out -- by escape, by ctrl-c, or by
        pressing enter on an empty line. Every caller must treat that as "went
        back", because an empty bundle list sends people to this prompt without
        their asking and they need a way out of it.
        """
        self._require_interactive(question, hint)
        from ferry.cli.prompts import run_path

        return run_path(question, theme=self.theme, default=default)


def _default_theme_name() -> str:
    """The theme name used when nothing overrides it."""
    return DEFAULT_THEME
