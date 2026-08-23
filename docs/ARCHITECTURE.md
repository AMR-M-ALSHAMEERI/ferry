# Architecture

Ferry has three layers with hard boundaries. Each is testable in isolation, and
each can be replaced without touching the others.

```
┌─────────────────────────────────────────────────────────┐
│  CLI layer (interactive prompts, menus, progress bars)  │
│  - typer (commands) + questionary (prompts)             │
│  - Knows about: user flow. Nothing about storage.       │
└─────────────────────────────────────────────────────────┘
                          │
┌─────────────────────────────────────────────────────────┐
│  Core layer (orchestration, bundle format, registry)    │
│  - Universal Conversation Schema (UCS)                  │
│  - Bundle packing/unpacking (zip + manifest)            │
│  - Adapter registry, conflict resolution, dry-run       │
└─────────────────────────────────────────────────────────┘
                          │
┌─────────────────────────────────────────────────────────┐
│  Adapter layer (per-tool I/O)                           │
│  - One adapter per tool: detect(), export(), import_()  │
│  - Knows about: that tool's files. Nothing else.        │
└─────────────────────────────────────────────────────────┘
```

**Why it matters:** the CLI can become a GUI without touching core. An adapter
can be rewritten without touching other adapters. New tools drop in as new
adapters.

## The Universal Conversation Schema (UCS)

Every adapter exports to UCS and imports from UCS. It is the contract that
decouples adapters from each other.

- Models: `src/ferry/ucs/models.py`
- Generated JSON schema: `schemas/ucs-<version>.json`
- Regenerate after model changes: `python scripts/gen_schema.py`

`tests/test_schema_current.py` fails if the checked-in schema drifts from the
models, so the two cannot silently diverge.

### Current version: 1.3

| Version | Change |
|---|---|
| 1.0 | Initial schema |
| 1.1 | Added the `thinking` content block (with optional `signature`) |
| 1.2 | Added the optional `provenance` block for cross-tool migration |
| 1.3 | Added the `image` content block; `tool_use` gained an optional `id` |

**1.2 bundles cannot be read by 1.3, and no converter is provided.** 1.2 was
never released, so there is no bundle anywhere that needs one.

**Schema rules that matter when writing an adapter:**

- **Additive only.** Adding fields is fine; removing or renaming one means a
  version bump. **A new member of a content-block union is not additive** — the
  models reject unknown `type` values outright, so a reader built for the older
  version refuses the document rather than ignoring the block. That is what
  1.3 was for.
- **An image block holds no bytes.** It carries an `attachment_id` pointing at
  an entry in `attachments[]`, where the file is stored and checksummed. Tools
  keep images inline as base64; re-embedding them in UCS would drag megabytes
  of encoded pixels through every read, diff and round-trip. The block exists
  to record *where in the conversation* the picture was, which is the one thing
  an attachment list cannot say.
- **Never fabricate data.** If the source tool did not store a timestamp, the
  UCS field is `null` — not `now()`, not `"unknown"`.
- **`source_raw` is the escape hatch** for lossless same-tool round trips.
- **Never present a foreign conversation as native.** If an adapter writes a
  conversation whose `source_tool` differs from the tool being written into, it
  must populate `provenance` and list every lossy conversion in
  `conversion_notes`.

All models use `extra="forbid"`, so an adapter typo fails loudly at validation
rather than silently dropping a field.

### Content blocks

Four discriminated types, keyed on `type`:

| Block | Carries |
|---|---|
| `text` | plain message text |
| `thinking` | reasoning text, plus an optional vendor `signature` |
| `tool_use` | tool `name` and `input` |
| `tool_result` | `tool_use_id` and `output` |

An unknown block type is rejected rather than accepted and ignored — a tool
emitting something UCS cannot represent is a problem to surface, not swallow.

## Bundles

A bundle is a directory while being built and a zip once packed:

```
manifest.json
conversations/<uuid>.json
attachments/<conversation-uuid>/<attachment-uuid>.<ext>
source_raw/<uuid>.bin          # optional
```

Implementation: `src/ferry/core/bundle.py`, `src/ferry/core/manifest.py`.

**Safety properties built into the Bundle class:**

- **Atomic writes.** Every write goes to `.tmp`, is fsynced, then renamed — a
  crash or full disk cannot leave a half-written file in place.
- **Refuses to overwrite** an existing archive without `force=True`.
- **Attachment checksums are verified on write**, not just recorded, so a
  truncated copy is caught immediately.
- **Zip extraction rejects path traversal.** Entries resolving outside the
  destination are refused, so a malicious bundle cannot write elsewhere on disk.
- **`validate()` returns a list of problems rather than raising** on the first
  one, so the CLI can show every fault in a bundle at once.
- **`has_conversation()`** lets an adapter skip already-written files when
  resuming an interrupted export.

## Adapter contract

Every adapter implements three methods (see PLAN.md §4 for the full signature):

- `detect()` — never raises; reports `installed`, `version`, `data_paths`,
  a conversation count estimate, and human-readable notes
- `export(dest_bundle_dir)` — yields `ExportEvent`s; must be resumable
- `import_(bundle_dir, options)` — yields `ImportEvent`s; backs up by default

`import_()` reads UCS files from a bundle. It has never required that those
files came from its own tool — which is what makes cross-tool migration
possible without new import machinery.

## Layout

```
src/ferry/
├── cli/          typer app, interactive menus
├── core/         bundle, manifest, orchestration
├── ucs/          pydantic schema models
├── adapters/     one subpackage per tool
├── compact/      LLM providers for conversation summarisation
└── config.py     API key / settings storage
scripts/          maintained tooling (schema generation, self-checks)
schemas/          generated JSON schema, checked in
```

## The CLI layer (M2)

The interface is deliberately interactive-first: running bare `ferry` scans for
installed assistants and drops into a menu, rather than requiring the user to
know command names. Flags exist underneath for scripting and for the planned
VS Code extension, but they are the secondary path.

```
ferry.cli.__init__   typer app, flags, non-TTY guard
ferry.cli.menu       scan screen + top-level menu loop
ferry.cli.ui         THE presentation layer — all output and prompts
ferry.cli.theme      palettes, icon sets, capability detection
ferry.adapters.base  Adapter ABC, events, registry
```

### Everything on screen goes through `ferry.cli.ui`

No module outside `ferry.cli` may import `rich` or `questionary` directly. The
moment an adapter prints its own coloured output, half the interface stops
respecting the active theme and the degradation rules below silently stop
applying. `UI` is the only sanctioned way to write to the terminal.

### Themes and degradation

Four themes ship: `harbor` (default), `compass`, `classic`, and `mono`.
Selection order is `--theme` → `FERRY_THEME` → `harbor`. An unrecognised name
falls back to the default rather than raising — a typo should never block
someone migrating their history.

Two independent capability checks then constrain the result:

| Condition | Effect |
|---|---|
| Not a TTY, `NO_COLOR`, or `TERM=dumb` | Whole theme drops to `mono` |
| Stream encoding cannot represent the glyphs | Icons drop to ASCII, **colours kept** |

The second check is a crash guard, not a cosmetic one. Windows consoles
routinely report `cp1252`, which has no mapping for `✔` — printing one there
raises `UnicodeEncodeError` and takes the process down. `supports_unicode()`
probes the stream's encoding with the full glyph set and downgrades the icons
alone, so a Windows user still gets a coloured interface.

### Adapters at M2

`ferry.adapters.base` defines the contract from PLAN.md §4. All four adapters
are `NotImplementedAdapter` stubs that report `installed=False` with the
milestone they arrive at. They deliberately do **not** invent conversation
counts — the scan screen tells the truth about what exists today.
