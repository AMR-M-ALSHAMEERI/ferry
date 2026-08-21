# Ferry

Back up and migrate your local AI assistant conversation history — export to a
portable bundle, re-import on a new machine, or carry it across to a different
assistant entirely — across **Claude Code**, **OpenAI Codex**,
**GitHub Copilot Chat**, and **Google Antigravity IDE**.

**Status:** pre-release, actively under construction. Not yet published to PyPI.

## Cross-tool migration — *planned, not yet implemented*

Ferry's Universal Conversation Schema is designed so a conversation is not
locked to the tool that created it: export from one assistant, import into a
different one — for example moving a Claude Code conversation into Codex.

When built, this will be **off by default** and gated behind an explicit flag,
because the conversion is **lossy**. Reasoning-block signatures cannot cross
vendors, and tool calls reference tools the target assistant does not have.
The intent is that Ferry shows you exactly what would be lost before writing
anything, marks the result as converted rather than passing it off as native,
and refuses conversions it cannot do safely instead of writing something broken.

Which tool pairs end up supported depends on what each tool's storage format
allows; some may not be feasible at all. This section will be replaced with the
actual supported-pair matrix once the feature exists.

## What Ferry does NOT do

Importing history makes conversations *visible and browsable* in the target
tool again, exactly as if you'd never switched machines. It does **not** make
the AI automatically "remember" the thread — no current tool re-loads full
conversation history into context on a new message. Ferry's optional
**Compact** feature is the workaround: it generates a context summary you can
paste as the first message in a new session.

This applies doubly to cross-tool migration: it gives you a readable transcript
in the target tool, not a session that tool's AI can pick up and continue. If
your goal is to *continue* the work elsewhere, use **Compact** — it is the
better tool for that job.

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

Full export → bundle → import walkthrough will be documented here once the
adapters are implemented.

## Supported tools

| Tool | Status |
|---|---|
| Claude Code | planned |
| OpenAI Codex | planned |
| GitHub Copilot Chat | planned |
| Google Antigravity IDE | planned |

## Safety

- Ferry never modifies your source conversation data — it only reads.
- Backups of the target tool's data are created by default before any import.
- Close the target IDE before importing.

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for contributing, and
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) (added at M7) once issues
start to surface.
