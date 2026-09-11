<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/AMR-M-ALSHAMEERI/ferry/main/docs/assets/ferry-logo-dark.svg">
    <img src="https://raw.githubusercontent.com/AMR-M-ALSHAMEERI/ferry/main/docs/assets/ferry-logo-light.svg" alt="Ferry: carry your conversations across" width="440">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/AMR-M-ALSHAMEERI/ferry/actions/workflows/ci.yml"><img src="https://github.com/AMR-M-ALSHAMEERI/ferry/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/ferry-cli/"><img src="https://img.shields.io/pypi/v/ferry-cli" alt="PyPI version"></a>
  <a href="https://pypi.org/project/ferry-cli/"><img src="https://img.shields.io/pypi/pyversions/ferry-cli" alt="Python versions"></a>
  <a href="https://github.com/AMR-M-ALSHAMEERI/ferry/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey" alt="Platforms: Windows, macOS, Linux">
</p>

<p align="center">
  Works with Claude Code, OpenAI Codex, GitHub Copilot Chat and Google Antigravity.
</p>

Ferry backs up the conversation history your AI coding assistants keep on your
computer, and puts it back: on the same machine, on a new one, or in a
different assistant altogether. It runs entirely on your machine and never
changes the history it reads from.

## Install

Ferry needs Python 3.11 or newer.

```bash
pip install ferry-cli
```

No Python? [uv](https://docs.astral.sh/uv/) installs Ferry together with a
Python of its own, without touching the rest of your system:

```bash
uv tool install ferry-cli
```

Either way, the command you run is `ferry`.

## Moving to a new laptop

On the old machine, export each assistant you use into a bundle. A bundle is an
ordinary folder you can copy anywhere.

```bash
ferry export --tool claude-code --output ferry-claude-code
```

Copy the folder to the new machine, close the assistant there, and import it.
The first run is a preview that writes nothing:

```bash
ferry import --bundle ferry-claude-code --tool claude-code --dry-run
```

```bash
ferry import --bundle ferry-claude-code --tool claude-code
```

That is the whole idea. Backing up, restoring and moving to another assistant
are all the same two steps: export to a bundle, then import from it.

## The menu

Run `ferry` on its own and it finds your assistants, tells you what it sees,
and offers everything as a menu you move through with the arrow keys:

```text
  ✓  Claude Code          2.1.266 · 4 conversations
  ✓  OpenAI Codex         0.146.0-alpha.3.1 · 5 conversations
  ✓  GitHub Copilot Chat  1.136.2 · 5 conversations
  ✓  Antigravity          2.12.2 · 2 conversations
```

Escape backs out of any screen without doing anything, and every screen that
writes starts on the option that writes nothing. The
[guide](https://github.com/AMR-M-ALSHAMEERI/ferry/blob/main/docs/GUIDE.md)
walks through each one.

## Supported assistants

| Assistant | Export | Import | Last checked with |
|---|---|---|---|
| Claude Code | yes | yes | 2.1.266 |
| OpenAI Codex | yes | yes | 0.146.0-alpha.3.1 |
| GitHub Copilot Chat | yes | yes | VS Code 1.136.2 |
| Google Antigravity | yes | yes | 2.12.2 |

None of these tools publish their storage formats. Ferry reads them as they
are, and [FORMATS.md](https://github.com/AMR-M-ALSHAMEERI/ferry/blob/main/docs/FORMATS.md)
records what was found and on which version. A tool update can change a format
without warning, so if something looks wrong after an update, the
[troubleshooting page](https://github.com/AMR-M-ALSHAMEERI/ferry/blob/main/docs/TROUBLESHOOTING.md)
is the place to start.

## Moving a conversation to a different assistant

A conversation from any of the four can be written into any of the others, and
you can open it there and carry on working in it. Ferry only converts when you
ask, tells you first what the conversion costs, and marks the result as
converted rather than passing it off as native.

Two things do not survive the trip. The assistant's private reasoning is signed
by the company whose model produced it, so no other assistant can accept it.
And tool calls, such as commands the old assistant ran, arrive as readable text
rather than as calls the new assistant can run again.
[CROSS-TOOL.md](https://github.com/AMR-M-ALSHAMEERI/ferry/blob/main/docs/CROSS-TOOL.md)
has the details.

## Letting an AI assistant drive Ferry

Ferry ships with a skill file that teaches an AI coding assistant when to use
Ferry and how, including the rules it must keep. Claude Code, OpenAI Codex,
GitHub Copilot Chat and Google Antigravity all read the same kind of file, and
one command installs it for every one of them Ferry finds on your computer:

```bash
ferry skill --install
```

After that you can simply ask, *"move my Claude Code chats to my new laptop"*,
and the assistant runs the commands, checking with you before anything that
writes. `ferry skill` on its own prints the file, for any other assistant.

## What Ferry does not do

**It moves conversations, not memory.** A conversation you restore or convert
keeps its own context, and you can pick it up where you left off. Some
assistants also keep separate notes about you that apply to every chat, and
those stay behind. Project instruction files such as `CLAUDE.md` and
`AGENTS.md` live in your project folder, so they travel with your code anyway.

It does not reach cloud chat history, such as claude.ai or chatgpt.com, only
what is stored on your computer. It does not move your project files either:
copy those the way you normally would.

If you would rather carry the gist of a long conversation than all of it,
**Compact** turns one conversation into a short document to paste into a new
session. It runs offline, with no account or model, and every line it writes is
either quoted from your conversation or counted from it.

## Safety

- **Your original history is never changed.** Exporting only reads.
- **No network calls.** Nothing Ferry reads ever leaves your computer.
- **Close the assistant before importing into it.** VS Code and Antigravity
  keep their conversation lists in memory and would undo an import made while
  they are open. Ferry checks and tells you.
- **Backups come first.** Anything an import replaces, and anything a delete
  removes, is copied into `~/.ferry/backups` beforehand. Ferry never deletes a
  backup on its own.
- **What is already there stays.** If an assistant already has a conversation,
  Ferry keeps its copy unless you choose otherwise.
- **Encryption is optional, and there is no recovery.** Ferry can seal a bundle
  into a single encrypted `.ferry` file. It asks for the passphrase twice and
  proves the file opens before offering to remove the unencrypted copy. Lose
  the passphrase and the bundle cannot be opened by anyone.
- **What Ferry imported, it can take back,** but only while nobody has worked
  in it since, and never a conversation of your own.

## Documentation

| Page | For |
|---|---|
| [Guide](https://github.com/AMR-M-ALSHAMEERI/ferry/blob/main/docs/GUIDE.md) | Every screen and every command |
| [Moving between assistants](https://github.com/AMR-M-ALSHAMEERI/ferry/blob/main/docs/CROSS-TOOL.md) | What converting costs, and what works |
| [Troubleshooting](https://github.com/AMR-M-ALSHAMEERI/ferry/blob/main/docs/TROUBLESHOOTING.md) | When something does not show up |
| [Storage formats](https://github.com/AMR-M-ALSHAMEERI/ferry/blob/main/docs/FORMATS.md) | Where each assistant keeps its history |
| [Architecture](https://github.com/AMR-M-ALSHAMEERI/ferry/blob/main/docs/ARCHITECTURE.md) and [adapters](https://github.com/AMR-M-ALSHAMEERI/ferry/blob/main/docs/ADAPTERS.md) | How Ferry is built |
| [Development](https://github.com/AMR-M-ALSHAMEERI/ferry/blob/main/docs/DEVELOPMENT.md) | Setting up to contribute |
| [Changelog](https://github.com/AMR-M-ALSHAMEERI/ferry/blob/main/CHANGELOG.md) | What changed in each release |

## License

MIT. See [LICENSE](https://github.com/AMR-M-ALSHAMEERI/ferry/blob/main/LICENSE).
