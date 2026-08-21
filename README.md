# Ferry

Back up and migrate your local AI assistant conversation history — export to a
portable bundle, re-import on a new machine — across **Claude Code**,
**OpenAI Codex**, **GitHub Copilot Chat**, and **Google Antigravity IDE**.

**Status:** pre-release, actively under construction. Not yet published to PyPI.

## What Ferry does NOT do

Importing history makes conversations *visible and browsable* in the target
tool again, exactly as if you'd never switched machines. It does **not** make
the AI automatically "remember" the thread — no current tool re-loads full
conversation history into context on a new message. Ferry's optional
**Compact** feature is the workaround: it generates a context summary you can
paste as the first message in a new session.

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
