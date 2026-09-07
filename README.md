# Ferry

Back up and migrate your local AI assistant conversation history — export to a
portable bundle, re-import on a new machine, or carry it across to a different
assistant entirely — across **Claude Code**, **OpenAI Codex**,
**GitHub Copilot Chat**, and **Google Antigravity IDE**.

**Status:** pre-release, actively under construction. Not yet published to PyPI.

## Cross-tool migration

Ferry's Universal Conversation Schema is designed so a conversation is not
locked to the tool that created it. **Three of the four tools are targets
today** — Claude Code, GitHub Copilot Chat and OpenAI Codex. A conversation
from any of the four is written into any of those three with every message and
every word intact, listed where that tool lists its own conversations, and
openable from the same place.

**Antigravity is refused**, and the reason is a property of that tool rather
than a missing feature: an Antigravity conversation is restored from the
original SQLite database it was exported with, and Ferry can carry one across
but cannot build one for a conversation that never had it. The refusal names
that reason on screen.

Conversion is **off by default**. When a bundle holds conversations from
another tool, Ferry counts what converting them would cost — signatures that
cannot be reissued, tool calls that become text — and asks, with the option
that converts nothing under the cursor. The result is marked as converted in
its `provenance` record rather than passed off as native.

**You can open a migrated conversation and keep working in it.** That was not
true when this was first written, and it was tested rather than assumed:
someone typed into a converted conversation in each of the three targets and
the assistant replied, reading the history it had been given.

What does not come with it is the *session* — the tool calls in a converted
conversation were made by a different assistant against a different set of
tools, and they are carried as readable text, not as calls the new tool can
re-run. If you want a short handoff to paste into a fresh session instead of
the whole history, **Compact** still does that better.

See [docs/CROSS-TOOL.md](docs/CROSS-TOOL.md) for the full table and what each
conversion costs.

## What Ferry does NOT do

Importing history makes conversations *visible and browsable* in the target
tool again, exactly as if you'd never switched machines. It does **not** make
the AI automatically "remember" the thread — no current tool re-loads full
conversation history into context on a new message. Ferry's optional
**Compact** feature is the workaround: it turns a conversation into a document
you can paste as the first message in a new session.

Compact runs **entirely on your machine**. There is no API key, no account, no
cost, and no model — it works offline, and it adds nothing to Ferry's install.
It also invents nothing: every line of what it produces is either quoted from
your conversation word for word or counted from it. What it gives up in
exchange is narrative. It can quote you the three facts; it cannot write the
sentence that joins them.

This applies to cross-tool migration too. A migrated conversation can be
continued — the assistant reads the transcript it was given and answers from
it — but nothing re-loads a whole history into a model's context on your
behalf, in any tool, converted or not.

## Install

```bash
pip install ferry-cli
```

(Not yet published — this will work once v0.1.0 ships.)

## Quickstart

```bash
ferry            # launch the interactive menu
ferry --version  # print the installed version
```

Arrow keys move, Enter chooses, Escape backs out of any screen without doing
anything. Ferry finds your assistants, and everything else is two steps:
**export to a bundle**, then **import from it** — on this machine, another
machine, or into a different assistant.

**[docs/GUIDE.md](docs/GUIDE.md) is the walkthrough**: every screen, what each
option does, and what it costs. Worth reading before the first import, because
that is the one part of Ferry that writes into your real conversation history.

## Supported tools

| Tool | Status |
|---|---|
| Claude Code | export and import |
| OpenAI Codex | export and import |
| GitHub Copilot Chat | export and import |
| Google Antigravity IDE | export and import |

Ferry counts the conversations **each application lists**, which is not the
same as the files on disk: an unused chat panel writes a file nobody had a
conversation in, and a subagent gets a file of its own that the tool never
shows you. Everything is still carried into the bundle; it is just not counted
as a conversation you had.

## Safety

- Ferry never modifies your source conversation data — it only reads.
- **Ferry makes no network calls at all.** Nothing it reads leaves this
  machine, and there is nothing to configure to keep it that way.
- **Preview first.** The import screen offers to show you exactly what would
  change, writing nothing, before you commit to it. That option is the one
  under the cursor.
- Anything an import would replace is copied into
  `~/.ferry/backups/<timestamp>/` first, with a record of where each file came
  from so it can be put back. Nothing prunes them.
- If a conversation is already there, Ferry leaves it alone unless you say
  otherwise. You can also keep both copies, or replace it.
- A bundle made on another machine records that machine's home folder; if it
  is not on this one, Ferry asks where those folders live now rather than
  restoring paths that point nowhere.
- Close the target IDE before importing.
- **Encryption is optional and irreversible if you lose the passphrase.** After
  an export, Ferry offers to seal the bundle into a single `.ferry` file
  (AES-256-GCM, passphrase stretched with scrypt). It asks twice, checks the
  sealed file opens before offering to remove the unencrypted copy, and never
  stores the passphrase anywhere. Sealing protects a bundle you carry or store
  — not the machine that made it.
- **Inspect** shows you what is in a bundle without importing it — every
  conversation, its size, and the folders it expects to find. Deleting lives on
  that same screen, because you delete something after looking at it. Deleting
  one conversation keeps a copy in `~/.ferry/backups` first; deleting a whole
  bundle does not, and says so.
- **A sealed bundle can be changed too.** Inspect offers to unseal it into a
  folder you can work with, leaving the `.ferry` file exactly where it is; or
  to delete a conversation and seal it again in one step. The second one never
  writes in place — the new file is written beside the old one, opened again
  with the same passphrase to prove it is readable, and only then replaces it.

See [docs/CROSS-TOOL.md](docs/CROSS-TOOL.md) for moving a conversation
between assistants, [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for
contributing, and
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) when something does not go
the way it should.
