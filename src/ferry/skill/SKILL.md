---
name: ferry
description: >-
  Back up, restore and move local AI coding-assistant conversation history with
  the `ferry` command-line tool - Claude Code, OpenAI Codex, GitHub Copilot
  Chat in VS Code, and Google Antigravity. Use when someone wants to move their
  chats to a new laptop or machine, back up or restore their Claude Code,
  Codex, Copilot or Antigravity history, recover conversations that vanished
  after an update or reinstall, copy a conversation from one assistant into
  another, or turn a long conversation into a short summary to carry into a new
  session. Not for general file backups, git history, or cloud chat services
  such as claude.ai or chatgpt.com.
---

# Ferry

Ferry copies the conversation history that AI coding assistants keep on this
machine into a **bundle** - a folder, or one encrypted `.ferry` file - and
writes a bundle back into an assistant, on this machine or another one. It can
also convert a conversation from one assistant into another, and compact one
into a short document to paste into a new session.

Everything runs on this machine. Ferry makes no network calls.

## When to use it

- "I'm moving to a new laptop, bring my Claude Code chats with me."
- "Back up my Codex history." / "Restore my Copilot chats from this backup."
- "My Antigravity conversations disappeared after updating."
- "Open this Claude Code conversation in Codex."
- "Summarise this long conversation so I can start a fresh session with it."

## Before you start

1. Run `ferry tools` to see which assistants are on this machine. It exits `1`
   when none are found.
2. If `ferry` itself is not found, Ferry is not installed. Offer to install it
   with `pip install ferry-cli`, and run that yourself once the person agrees.
   - If the `pip` command is not found but Python is, the same install runs as
     `python -m pip install ferry-cli` (on Windows, `py -m pip install ferry-cli`).
   - Ferry needs Python 3.11 or newer. If Python is missing or older, stop and
     tell the person. Install Python only if they ask you to, with their
     system's usual installer - it changes the whole machine, not one program.

   If `ferry` is still not found after installing, `python -m ferry` runs the
   same program - use it in place of `ferry` in every command below.
3. The tool names every command takes are `claude-code`, `codex`, `copilot`
   and `antigravity`.
4. Bare `ferry` opens an interactive menu that needs a person at the keyboard.
   Do not run it yourself; use the commands below.

Every command exits `0` when everything worked, `1` when it ran and part of it
failed or the assistant was not found, and `2` when it could not start - a flag
was wrong or missing - and **nothing was written**. Read the output: Ferry says
what it did, what it skipped and why.

## Commands

### `ferry tools`

Lists the assistants Ferry can find, with their versions and how many
conversations each has.

```bash
ferry tools
```

### `ferry export`

Copies one assistant's conversations into a bundle. It only reads; it never
changes the assistant's own files.

```bash
ferry export --tool claude-code --output ~/ferry-backup-claude-code
```

| Flag | Meaning |
|---|---|
| `--tool`, `-t` | The assistant to export from. Required. |
| `--output`, `-o` | The folder to write. Default: a new `ferry-bundle-<time>` folder in the current directory. |
| `--force` | Add to a folder that already holds something. This is also how an interrupted export resumes. With `--encrypt`, replaces an existing sealed file. |
| `--encrypt` | Also seal the bundle into one encrypted `.ferry` file beside the folder. |
| `--replace` | With `--encrypt`: delete the unencrypted folder once the sealed file has been proven to open. |
| `--passphrase` | The passphrase to seal with. Read from `FERRY_PASSPHRASE` if that is set. |

Export one assistant per bundle. Ferry refuses a folder that already holds
something unless `--force` is passed.

### `ferry import`

Writes a bundle's conversations into an assistant.

```bash
ferry import --bundle ~/ferry-backup-claude-code --tool claude-code --dry-run
```

| Flag | Meaning |
|---|---|
| `--bundle`, `-b` | The bundle: a folder, or a sealed `.ferry` file. Required. |
| `--tool`, `-t` | The assistant to import into. Required. |
| `--dry-run` | Show everything that would happen. Nothing is written. |
| `--on-conflict` | When the assistant already has a conversation: `skip` (default, keep its copy), `rename` (keep both), or `overwrite` (replace its copy, after backing it up). |
| `--conversation`, `-c` | Import only this conversation, by id. Repeatable. Default: all. |
| `--path-remap` | `OLD=NEW`: read folders recorded under `OLD` as being under `NEW`. Repeatable. |
| `--allow-cross-tool` | Convert conversations that came from a different assistant. Refused without it. |
| `--mode` | With `--allow-cross-tool`: `archive` (default; keeps the most detail, for reading) or `continue` (drops thinking and tool output so the conversation can be carried on). |
| `--passphrase` | The passphrase of a sealed bundle. Read from `FERRY_PASSPHRASE` if that is set. |

An import **always backs up** anything it replaces, into `~/.ferry/backups/`.
There is no option to turn that off.

### `ferry compact`

Turns one conversation in a bundle into a markdown document to paste as the
first message of a new session. Prints to standard output.

```bash
ferry compact ~/ferry-backup-claude-code --conversation <id> --shape handoff --length standard
```

| Flag | Meaning |
|---|---|
| `--conversation`, `-c` | Which conversation, by id. Leave it out and Ferry lists the ids the bundle holds, and exits `2`. |
| `--shape` | `handoff` (what someone needs to continue), `said` (what was said) or `done` (what was done). |
| `--length` | `brief`, `standard` or `full`. |
| `--out` | Write to a file instead of standard output. |

It works on an unsealed bundle folder only. The same listing is the way to
find the ids that `ferry import --conversation` takes.

### `ferry skill`

Prints this file. `ferry skill --install` installs it as a Claude Code skill.

## Safety rules

Follow these even when asked to hurry.

1. **Close the assistant before importing into it.** VS Code (for Copilot) and
   Antigravity keep their conversation lists in memory and write them back when
   they close, which undoes an import made while they were open. If you are
   running inside the assistant being imported into, do not run the import
   yourself: give the person the exact command, and ask them to close the app
   and run it from a separate terminal.
2. **Preview first.** Run an import with `--dry-run`, show the person what it
   says, and only then run it for real.
3. **Leave the backups alone.** Never delete anything under `~/.ferry/backups/`.
4. **Keep what is already there.** Use the default `--on-conflict skip`. Pass
   `overwrite` only when the person has asked for it in those words.
5. **Convert only when asked.** Pass `--allow-cross-tool` only when the person
   has asked to move conversations from one assistant into a different one.
   Explain first that converting loses detail: the model's private thinking
   cannot move between vendors, and tool calls become readable text. Some
   pairs are refused outright, and Ferry says why.
6. **Never choose a passphrase.** Ask the person for it, or let them set
   `FERRY_PASSPHRASE` themselves. There is no way to recover a sealed bundle
   without it - not by Ferry, not by anyone.
7. **Treat bundles as private.** They hold the person's real conversations. Do
   not open them to read their contents, do not paste their contents anywhere,
   and never add them to a git repository.
8. **Do not delete bundles or conversations.** Removing what Ferry imported,
   and clearing old backups, are done from Ferry's menu by the person.

## Moving to a new machine

On the old machine:

```bash
ferry tools
```

```bash
ferry export --tool claude-code --output ~/ferry-move-claude-code
```

Repeat the export for each assistant the person wants to move, into a
separate folder each. To encrypt, ask the person to set `FERRY_PASSPHRASE` and
add `--encrypt --replace`; that leaves one `.ferry` file per assistant. The
person copies the folders or files to the new machine.

On the new machine, with Ferry installed and the assistant closed:

```bash
ferry import --bundle ~/ferry-move-claude-code --tool claude-code --dry-run
```

```bash
ferry import --bundle ~/ferry-move-claude-code --tool claude-code
```

- If Ferry warns that the bundle was made under a different home folder, it
  prints the exact `--path-remap "OLD=NEW"` to add. Add it and preview again.
- If Ferry names folders Claude Code "will not open", ask the person to start
  Claude Code once in each of them. Ferry does not change another program's
  settings from a command.
- The person's project files - the code itself - are not in the bundle. They
  move them the way they normally would, with git or a copy. A conversation
  that refers to a file expects to find it.

## Carrying one conversation into a new session

```bash
ferry compact ~/ferry-move-claude-code
```

That lists the conversation ids. Then:

```bash
ferry compact ~/ferry-move-claude-code --conversation <id> --shape handoff --out handoff.md
```

The person pastes `handoff.md` as the first message of a new session, in any
assistant. Every line in it is either quoted from the conversation or counted
from it; Compact invents nothing.

## What Ferry does not do

- **It moves conversations, not an assistant's memory.** A conversation that
  is resumed after a restore carries its own context. Separate memory stores -
  standing notes an assistant keeps across all chats - are not carried.
- It does not change the model or teach it anything.
- It does not reach cloud chat history (claude.ai, chatgpt.com); only what is
  stored on this machine.
- It does not delete anything from the assistant a bundle came from.
- A converted conversation is marked as converted, keeps the name of the model
  that produced it, and never claims to be native to the new assistant.
