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

from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

import questionary
from questionary import Choice
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.theme import Theme as RichTheme

from ferry.cli.theme import DEFAULT_THEME, Capability, Theme, detect_capability, resolve_theme

__all__ = ["UI", "NonInteractiveError"]


class NonInteractiveError(RuntimeError):
    """Raised when a prompt is attempted without a usable terminal.

    Carries the flag-based equivalent so the caller can tell the user exactly
    what to run instead of leaving them stuck.
    """

    def __init__(self, action: str, hint: str) -> None:
        self.action = action
        self.hint = hint
        super().__init__(f"{action} needs an interactive terminal. {hint}")


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
        table.add_column(width=len(icons.error))
        table.add_column()
        table.add_column()
        for row in rows:
            if row.installed:
                mark = self._style("ferry.success", icons.success)
                name = self._style("ferry.text", row.display_name)
                detail = self._style("ferry.accent", row.detail)
            else:
                mark = self._style("ferry.dim", icons.error)
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
        with self.console.status(f"[ferry.dim]{label}[/ferry.dim]", spinner="dots"):
            yield

    @contextmanager
    def progress(self, label: str, total: int) -> Iterator[object]:
        """Show a progress bar for a known amount of work.

        Yields an object with ``advance(n=1)``. Under ``mono`` or when piped,
        the bar is suppressed and only start/finish lines are printed.
        """
        if not self.theme.uses_color or not self.interactive:
            self.console.print(f"  {label} (0/{total})")

            class _Silent:
                def __init__(self) -> None:
                    self.done = 0

                def advance(self, n: int = 1) -> None:
                    self.done += n

            silent = _Silent()
            yield silent
            self.console.print(f"  {label} ({silent.done}/{total})")
            return

        icons = self.theme.icons
        with Progress(
            SpinnerColumn(style="ferry.primary"),
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
                def advance(self, n: int = 1) -> None:
                    prog.advance(task, n)

            _ = icons
            yield _Live()

    # ---------- prompts ----------

    def _require_interactive(self, action: str, hint: str) -> None:
        """Refuse to prompt when there is no terminal to prompt on."""
        if not self.interactive:
            raise NonInteractiveError(action, hint)

    def select(
        self,
        question: str,
        choices: Sequence[tuple[str, str]],
        *,
        hint: str = "",
        allow_filter: bool = True,
    ) -> str | None:
        """Ask the user to pick one option.

        Args:
            question: The prompt text.
            choices: ``(value, label)`` pairs, in display order.
            hint: Flag-based equivalent, shown if there is no terminal.

        Returns:
            The chosen value, or ``None`` if the user cancelled.
        """
        self._require_interactive(question, hint)
        from ferry.cli.prompts import SelectorItem, run_select

        return run_select(
            question,
            [SelectorItem(value, label) for value, label in choices],
            theme=self.theme,
            allow_filter=allow_filter,
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

    def path(self, question: str, *, default: str = "", hint: str = "") -> str | None:
        """Ask for a filesystem path, with tab completion."""
        self._require_interactive(question, hint)
        answer = questionary.path(question, default=default, qmark=self.theme.icons.info).ask()
        return answer if isinstance(answer, str) else None


def _default_theme_name() -> str:
    """The theme name used when nothing overrides it."""
    return DEFAULT_THEME
