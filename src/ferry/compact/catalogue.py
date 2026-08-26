"""What a tool call did, and to what.

Compact's ledgers -- files touched, commands run -- need one question answered
for every ``tool_use`` block: *what kind of thing was this, and what was its
target?* The four tools answer it in four unrelated ways, so the mapping lives
here as data rather than as branches scattered through the extractor.

Measured from the human's own stores, not assumed (``spikes/probe_tool_inputs.py``
and ``spikes/probe_targets.py`` -- keys and markers only, no content):

======================  =========================================
``claude-code``         ``file_path`` / ``command``, structured
``codex``               ``raw`` for ``exec``, patch headers for edits
``copilot``             ``ferry_invocation_uris``, ``commandLine.original``
``antigravity``         one ``detail`` string, path inside the prose
======================  =========================================

**An unknown pair is ordinary, not exceptional.** Forty-one distinct tool names
appeared across nineteen conversations and the list grows with every release of
every tool. Anything unrecognised is :data:`OTHER` -- counted, never guessed
at, never a crash.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote

__all__ = ["OTHER", "URIS_KEY", "Call", "Fidelity", "Kind", "describe", "fidelity", "known_tools"]

Kind = Literal["read", "write", "command", "search", "other"]

URIS_KEY = "ferry_invocation_uris"
"""Where the Copilot reader puts the files a call named.

Spelled out here rather than imported from the adapter: this module reads
*bundles*, which may have been written by an older Ferry or on another machine.
Importing would tie the catalogue's idea of the key to the running adapter's,
and the key in a bundle is whatever wrote that bundle.
"""


@dataclass(frozen=True)
class Call:
    """One tool call, reduced to the facts a ledger needs."""

    kind: Kind = "other"
    paths: tuple[str, ...] = ()
    command: str = ""
    """Empty when the tool records no command line -- a real case, not a
    failure. Antigravity's ``RUN_COMMAND`` stores the shell and the binary name
    and not the line that was typed; see the M7c spec section 5.3."""


OTHER = Call()


# --------------------------------------------------------------------------
# reading a target out of the shapes each tool uses
# --------------------------------------------------------------------------

_PATH_IN_PROSE = re.compile(
    r"[A-Za-z]:[\\/][\w.\-\\/ ]{2,200}"
    r"|(?:[\w.\-]+[\\/])+[\w.\-]+\.\w{1,6}"
    r"|\b[\w.\-]{1,50}\.(?:py|md|ts|tsx|js|jsx|json|toml|yml|yaml|txt|rs|go|java"
    r"|c|h|cpp|css|html|sh|ps1|sql|jsonl|cfg|ini)\b"
)
"""A path **inside** a sentence.

Deliberately unanchored. The first Antigravity spike anchored this pattern,
demanded that the whole string be a path, and concluded that ``VIEW_FILE``
almost never names a file. Searching *inside* the same strings found a path in
**115 of 115** of them. The anchors were the entire difference between a "no"
and a "yes", so this note exists to stop anyone putting them back.
"""

_PATCH_HEADER = re.compile(
    r"^\*\*\* (?:Update|Add|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.MULTILINE
)
"""Codex's ``apply_patch`` envelope.

The markers, counted over real patches: ``Update File`` 544, ``Add File`` 108,
``Delete File`` 8, ``Move to`` 5. A fixed vocabulary -- this parses a format
rather than guessing at prose.
"""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _one(arguments: dict[str, Any], key: str) -> tuple[str, ...]:
    found = _text(arguments.get(key))
    return (found,) if found else ()


def _uris(arguments: dict[str, Any]) -> tuple[str, ...]:
    """The files a Copilot call named, as paths.

    ``file:///c%3A/Users/...`` is a URI; the thing a person recognises is
    ``c:/Users/...``. Percent-decoding and dropping the slash that precedes a
    drive letter is the whole of the conversion.
    """
    raw = arguments.get(URIS_KEY)
    if not isinstance(raw, list):
        return ()
    found: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item:
            continue
        text = unquote(item).removeprefix("file://")
        if len(text) > 2 and text[0] == "/" and text[2] == ":":
            text = text[1:]
        found.append(text)
    return tuple(found)


def _patch_paths(arguments: dict[str, Any]) -> tuple[str, ...]:
    raw = _text(arguments.get("raw"))
    if not raw:
        return ()
    found: list[str] = []
    for update, moved in _PATCH_HEADER.findall(raw):
        target = (update or moved).strip()
        if target:
            found.append(target)
    return tuple(dict.fromkeys(found))


def _prose_paths(arguments: dict[str, Any]) -> tuple[str, ...]:
    """The paths mentioned in an Antigravity step's one line of text."""
    found = _PATH_IN_PROSE.findall(_text(arguments.get("detail")))
    return tuple(dict.fromkeys(match.strip() for match in found if match.strip()))


def _terminal_command(arguments: dict[str, Any]) -> str:
    """Copilot's command line, which is nested one level down.

    ``commandLine`` holds ``original`` -- what was asked for -- beside
    ``forDisplay``, which VS Code may have shortened to fit the screen. The
    original is the one that ran.
    """
    line = arguments.get("commandLine")
    if isinstance(line, dict):
        return _text(line.get("original")) or _text(line.get("forDisplay"))
    return _text(line)


# --------------------------------------------------------------------------
# the catalogue itself
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    kind: Kind
    path_key: str = ""
    command_key: str = ""
    reader: str = ""
    """Named, not held as a function, so the table stays comparable data. The
    functions are in :data:`_READERS`."""


_READERS = {
    "uris": _uris,
    "patch": _patch_paths,
    "prose": _prose_paths,
}

_CATALOGUE: dict[str, dict[str, _Entry]] = {
    "claude-code": {
        "Write": _Entry("write", path_key="file_path"),
        "Edit": _Entry("write", path_key="file_path"),
        "MultiEdit": _Entry("write", path_key="file_path"),
        "NotebookEdit": _Entry("write", path_key="notebook_path"),
        "Read": _Entry("read", path_key="file_path"),
        "NotebookRead": _Entry("read", path_key="notebook_path"),
        "Bash": _Entry("command", command_key="command"),
        "PowerShell": _Entry("command", command_key="command"),
        "Glob": _Entry("search"),
        "Grep": _Entry("search"),
    },
    "codex": {
        "exec": _Entry("command", command_key="raw"),
        "shell_command": _Entry("command", command_key="command"),
        "apply_patch": _Entry("write", reader="patch"),
        "view_image": _Entry("read", path_key="path"),
    },
    "copilot": {
        "copilot_readFile": _Entry("read", reader="uris"),
        "copilot_findFiles": _Entry("search"),
        "copilot_findTextInFiles": _Entry("search"),
        "copilot_searchCodebase": _Entry("search"),
        "run_in_terminal": _Entry("command", command_key="commandLine"),
    },
    "antigravity": {
        # VIEW_FILE names its file in the step's own text, 115 times out of 115.
        "VIEW_FILE": _Entry("read", reader="prose"),
        # RUN_COMMAND and CODE_ACTION are deliberately bare. Antigravity does
        # record *something* for both, but the spike measured it at 34% and
        # 8.6% purity -- a bare binary name, and fragments of the contents of
        # the files being edited. A ledger built on that would be wrong often
        # enough to mislead, and this feature's one promise is that nothing in
        # it is invented.
        "RUN_COMMAND": _Entry("command"),
        "CODE_ACTION": _Entry("write"),
        "LIST_DIRECTORY": _Entry("search"),
        "GREP_SEARCH": _Entry("search"),
    },
}


def describe(source_tool: str, name: str, arguments: dict[str, Any] | None) -> Call:
    """What one tool call did, and to what.

    Args:
        source_tool: The conversation's ``source_tool``.
        name: The ``ToolUseBlock`` name.
        arguments: Its ``input``.

    Returns:
        A :class:`Call`. An unrecognised tool, and a recognised tool whose
        target is missing, both give back empty ``paths`` and ``command``
        rather than a guess.
    """
    entry = _CATALOGUE.get(source_tool, {}).get(name)
    if entry is None:
        return OTHER
    arguments = arguments if isinstance(arguments, dict) else {}

    paths: tuple[str, ...] = ()
    if entry.reader:
        paths = _READERS[entry.reader](arguments)
    elif entry.path_key:
        paths = _one(arguments, entry.path_key)

    command = ""
    if entry.command_key == "commandLine":
        command = _terminal_command(arguments)
    elif entry.command_key:
        command = _text(arguments.get(entry.command_key))

    return Call(kind=entry.kind, paths=paths, command=command)


def known_tools(source_tool: str) -> frozenset[str]:
    """The tool names the catalogue recognises for one tool."""
    return frozenset(_CATALOGUE.get(source_tool, {}))


# --------------------------------------------------------------------------
# what a ledger built from this tool is worth
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Fidelity:
    """How much of the record a given tool actually keeps.

    Compact states this in the document rather than implying that every tool
    yields the same thing. Where a ledger cannot be built, the section is
    **omitted with this reason** -- printing "Files touched: none" for a
    session that touched forty would be a lie told by omission.
    """

    tier: str
    files: str = ""
    commands: str = ""


_FIDELITY: dict[str, Fidelity] = {
    "claude-code": Fidelity(tier="complete"),
    "codex": Fidelity(tier="good"),
    "copilot": Fidelity(
        tier="good",
        files=(
            "Copilot records a path only for the files it read. Its search tools "
            "record no results, so files reached through a search are not listed."
        ),
    ),
    "antigravity": Fidelity(
        tier="partial",
        files=(
            "Antigravity records the files it read. What it created or changed is "
            "not in its record, so this is what was looked at, not what was written."
        ),
        commands=(
            "Antigravity records the shell and the program name, not the command "
            "line that was run, so there is nothing here to quote."
        ),
    ),
}

_UNKNOWN = Fidelity(
    tier="unknown",
    files="Ferry has no catalogue for this tool, so no file or command list was built.",
    commands="Ferry has no catalogue for this tool, so no file or command list was built.",
)


def fidelity(source_tool: str) -> Fidelity:
    """What a ledger built from this tool's records is worth."""
    return _FIDELITY.get(source_tool, _UNKNOWN)
