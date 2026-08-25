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

### Looking inside a bundle, and deleting from it

`ferry.core.summary.summarise()` describes a bundle without importing it: what
each conversation is, when it happened, how much of the bundle it accounts for,
and **which folders the conversations were recorded in** — the list the import
screen cannot afford to compute, since answering it means opening every
conversation file and one Codex conversation is 53 MB.

It reads with `json.loads`, deliberately **not** through the UCS models. A
53 MB conversation validated through pydantic builds tens of thousands of
objects to answer six questions, and a conversation that *fails* validation
still has to appear in the list — a bundle you cannot read is precisely the one
you need to look at, and possibly the one you want to delete.

**A count here is a count of the bundle, not of the tool.** A Codex bundle
holding five conversations and one subagent thread reports six, because six
conversation documents is what it holds. The scan screen reports what the
application lists; this screen reports what the file contains. Both are true
and they are answers to different questions.

**Deleting** (`Bundle.delete_conversation`, `delete_bundle`) is the only thing
Ferry does after which the data is simply gone — every other write leaves the
source tool holding the original. Three properties follow:

- **A conversation is not one file.** The document, `attachments/<uuid>/`,
  `source_raw/<uuid>.bin` and its sidecars go together, and the manifest count
  and `tools_included` move with them. Removing the document alone leaves a
  bundle that still validates while carrying orphaned megabytes — for an
  Antigravity conversation, the original database, which is most of its size.
- **A copy is kept first when deleting one conversation**, and a failed backup
  aborts the delete rather than being skipped. Deleting a *whole* bundle
  defaults the other way: a bundle is routinely gigabytes, and copying one in
  order to delete it is a rename the user did not ask for.
- **`delete_bundle` refuses any directory without a `manifest.json`.** It is
  the only place Ferry removes a tree it did not create, and that check is what
  stands between a mistyped path and someone's Documents folder.

### Sealed bundles

A **sealed bundle** is a single `.ferry` file: the packed bundle, encrypted
whole with AES-256-GCM under a key derived from a passphrase by scrypt.
Nothing about it is readable without that passphrase — not the conversation
count, not which tools it came from, not the date it was made.

Implementation: `src/ferry/core/crypto.py` (framing) and
`src/ferry/core/sealed.py` (what gets sealed, and when).

**Why sealing rather than encrypting each file as it is written.** Encrypting
in place would mean every read and write path learning about keys: four
adapters, `validate()`, `inspect`, the attachment checksums. Nine places, all
of them recently stabilised, and a mistake in any of them is *silent* — files
that look encrypted and can never be opened again. Sealing puts encryption in
one auditable place that either works or visibly does not.

**What that costs, and it is stated in the UI too.** The unencrypted bundle
exists on disk while it is being made, and again under `~/.ferry/open` while
Ferry reads a sealed one. Both are deleted. On most filesystems the blocks are
not overwritten, so a forensic tool could recover them until that space is
reused. **Sealing protects a bundle you carry or store; it does not protect
the machine that made it.**

**The framing, because AES-GCM alone does not give these:**

- **A frame cannot be moved.** Its number is part of its nonce.
- **The end is authenticated.** Each 1 MiB frame says whether it is the last,
  and that flag is covered by the tag. Without it, cutting a file *between*
  frames leaves every remaining frame intact and decryption simply stops at
  EOF — handing back two thirds of a conversation as though it were whole.
- **A failure leaves nothing.** Output is renamed into place only once the
  last frame authenticates. Half a plaintext is worse than none, because it
  reads as content.

**Operational rules the flow enforces:**

- The passphrase is asked twice. There is no recovery.
- The sealed file is opened again with the same passphrase **before** the
  unencrypted bundle can be deleted, and deleting it is a separate question.
- A sealed bundle is opened **read-only** by `inspect`. Deleting from inside
  one would mean unseal, edit, reseal — three chances to lose the only copy.
- Ferry never stores a passphrase, and never puts one in a log or an error.

Measured on the reference machine: a 36.2 MB Antigravity bundle seals to
11.7 MB in 1.8 s and opens in 1.8 s; key derivation is 0.26 s, once per bundle.

## Adapter contract

Every adapter implements three methods (see PLAN.md §4 for the full signature):

- `detect()` — never raises; reports `installed`, `version`, `data_paths`,
  a conversation count estimate, and human-readable notes
- `export(dest_bundle_dir)` — yields `ExportEvent`s; must be resumable
- `import_(bundle_dir, options)` — yields `ImportEvent`s; backs up by default

`import_()` reads UCS files from a bundle. It has never required that those
files came from its own tool — which is what makes cross-tool migration
possible without new import machinery.

### `ImportOptions`, and holding every adapter to them

| Option | Meaning |
|---|---|
| `backup` | Copy anything about to be overwritten into `~/.ferry/backups` first. On by default. |
| `dry_run` | Report what would happen. **Write nothing.** |
| `on_conflict` | `skip` (default), `rename`, or `overwrite`. |
| `path_remap` | `(old_prefix, new_prefix)` pairs, applied in order, first match wins. |
| `allow_cross_tool` | Permit a conversation from a different tool. Off by default. |

Every adapter was tested against these in its own test file — except Copilot,
whose import ignored the options object entirely for two milestones. **A dry
run into Copilot Chat would have written**: a row into VS Code's chat index and
a transcript file. Nothing caught it, because the CLI passed the defaults and
never set `dry_run`, so the option was only reachable from tests nobody had
written.

`tests/test_import_contract.py` now asks all four adapters the same questions,
so a fifth adapter inherits the whole contract by being added to `BUILDERS`.

### What `rename` means

All four tools identify a conversation by its id **and put that id in the
filename**. Writing `<uuid>-1.jsonl` that still says `sessionId: <uuid>` inside
produces a session contradicting itself — and, in Claude Code, one sharing its
spilled tool output with the file it was trying not to overwrite.

So `rename` imports a **new identity**: a fresh id carried into the filename,
the records, and anything keyed on it. Antigravity refuses instead, with a
reason — its id is written through protobuf blobs Ferry has no schema to
re-identify, and a half-re-identified database is worse than no copy.

### Backups

`ferry.core.backup` writes one directory per Ferry run —
`~/.ferry/backups/<timestamp>/<tool>/` — with a `manifest.jsonl` recording each
file's original path. Each adapter previously had its own copy of this helper,
computing the timestamp per file (so an import crossing a second boundary split
across directories) and recording nothing about where the file came from, which
made the copies useless for the one thing a backup is for. Antigravity wrote
its `.bak` *beside the original*, inside the store Antigravity itself reads.

Nothing prunes backups. A tool that quietly deletes copies it made of someone's
conversation history has misunderstood its job.

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
