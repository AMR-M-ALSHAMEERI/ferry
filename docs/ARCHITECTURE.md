# Architecture

Ferry has three layers with firm boundaries between them. Each can be tested on
its own, and each can be replaced without touching the others.

```text
┌─────────────────────────────────────────────────────────┐
│  CLI layer: the menu, the commands, the screens         │
│    typer for commands, prompt_toolkit and rich          │
│    Knows about the person. Nothing about storage.       │
└─────────────────────────────────────────────────────────┘
                          │
┌─────────────────────────────────────────────────────────┐
│  Core layer: bundles, the schema, backups, records      │
│    The Universal Conversation Schema (UCS)              │
│    Bundles, sealing, backups, provenance                │
└─────────────────────────────────────────────────────────┘
                          │
┌─────────────────────────────────────────────────────────┐
│  Adapter layer: one adapter per tool                    │
│    detect(), export(), import_(), and taking back       │
│    Knows about that tool's files. Nothing else.         │
└─────────────────────────────────────────────────────────┘
```

**Why it matters:** the interface could become a graphical one without touching
the core, an adapter can be rewritten without touching the others, and a new
tool arrives as a new adapter.

## The Universal Conversation Schema (UCS)

Every adapter exports to UCS and imports from it. It is the contract that keeps
the adapters independent of each other.

- Models: `src/ferry/ucs/models.py`
- Generated JSON schema: `schemas/ucs-<version>.json`
- After changing the models: `python scripts/gen_schema.py`

`tests/test_schema_current.py` fails if the checked-in schema no longer matches
the models, so the two cannot quietly drift apart.

### Current schema version: 1.3

This is the version of the conversation format, recorded as `ucs_version` in
every conversation file inside a bundle. It is not Ferry's own version, which is
0.1.0 and is what `ferry --version` prints. The two move independently: a new
release of Ferry does not change the format unless the format itself changes.

| Schema version | Change |
|---|---|
| 1.0 | The first schema |
| 1.1 | Added the `thinking` content block, with an optional `signature` |
| 1.2 | Added the optional `provenance` block, for conversions between tools |
| 1.3 | Added the `image` content block; `tool_use` gained an optional `id` |

**Version 1.3 cannot read 1.2 bundles, and there is no converter.** Version 1.2
was never released, so no bundle anywhere needs one.

**Rules that matter when writing an adapter:**

- **Only ever add.** Adding a field is fine. Removing or renaming one needs a
  new version. **A new kind of content block is not an addition,** because the
  models reject a `type` they do not know, so a reader built for the older
  version refuses the whole document rather than skipping the block. That is
  what version 1.3 was for.
- **An image block holds no image.** It carries an `attachment_id` pointing to
  an entry in `attachments[]`, where the file is stored and checksummed. The
  tools keep images inline as base64, and copying them into UCS would drag
  megabytes of encoded pixels through every read and comparison. The block
  exists to record *where in the conversation* the picture was, which an
  attachment list alone cannot say.
- **Never make data up.** If the source tool did not store a timestamp, the UCS
  field is `null`, not `now()` and not `"unknown"`.
- **`source_raw` is the way out** when UCS cannot hold something, and it is
  what makes restoring into the same tool exact.
- **Never pass a foreign conversation off as native.** An adapter writing a
  conversation whose `source_tool` differs from its own tool must fill in
  `provenance` and list everything lost in `conversion_notes`.

All models use `extra="forbid"`, so a typo in an adapter fails loudly when the
document is checked, instead of silently dropping a field.

### Content blocks

Five types, told apart by `type`:

| Block | Carries |
|---|---|
| `text` | Plain message text |
| `thinking` | Reasoning text, and optionally the vendor's `signature` |
| `tool_use` | The tool's `name` and `input`, and optionally an `id` |
| `tool_result` | The `tool_use_id` it answers, and the `output` |
| `image` | An `attachment_id` naming the file in `attachments[]` |

An unknown block type is rejected rather than accepted and ignored. A tool
producing something UCS cannot represent is a problem to show, not to hide.

## Bundles

A bundle is a folder:

```text
manifest.json
conversations/<uuid>.json
attachments/<conversation-uuid>/<attachment-uuid>.<ext>
source_raw/<uuid>.bin          optional
```

Sealing packs that folder into a zip and encrypts it.

The folder layout has a version of its own, `bundle_version` in
`manifest.json`, currently 1.0. So a bundle carries two format versions, one
for its layout and one for each conversation in it, and neither is Ferry's
version.

The code is in `src/ferry/core/bundle.py` and `src/ferry/core/manifest.py`.

**Safety built into the `Bundle` class:**

- **Writes are atomic.** Every file is written to a temporary name, flushed to
  disk, then renamed into place, so a crash or a full disk cannot leave half a
  file behind.
- **An existing archive is never overwritten** without `force=True`.
- **Attachment checksums are checked as they are written,** not only recorded,
  so a truncated copy is caught at once.
- **Unzipping refuses to escape the destination.** An entry that would land
  outside it is refused, so a malicious bundle cannot write elsewhere on disk.
- **`validate()` returns a list of problems** rather than stopping at the first
  one, so the interface can show every fault in a bundle at once.
- **`has_conversation()`** lets an adapter skip what is already written when an
  interrupted export runs again.

### Looking inside a bundle, and deleting from it

`ferry.core.summary.summarise()` describes a bundle without importing it: what
each conversation is, when it happened, how much of the bundle it takes up, and
**which folders the conversations were recorded in**. The import screen cannot
afford to work that last one out, because it means opening every conversation
file, and one Codex conversation can be 53 MB.

It reads with `json.loads` and deliberately does **not** go through the UCS
models. Checking a 53 MB conversation through pydantic builds tens of thousands
of objects to answer six questions, and a conversation that *fails* checking
still has to appear in the list. A bundle you cannot read is exactly the one
you need to look at, and perhaps the one you want to delete.

**A count here is a count of the bundle, not of the tool.** A Codex bundle
holding five conversations and one subagent reports six, because it holds six
conversation documents. The scan screen reports what the application lists;
this screen reports what the file holds. Both are true, and they answer
different questions.

**Deleting** (`Bundle.delete_conversation`, `delete_bundle`) is the only thing
Ferry does after which data is simply gone, because every other write leaves
the source tool holding the original. Three things follow:

- **A conversation is more than one file.** The document,
  `attachments/<uuid>/`, `source_raw/<uuid>.bin` and its companions go
  together, and the manifest's count and `tools_included` move with them.
  Removing the document alone would leave a bundle that still checks out while
  carrying orphaned megabytes: for an Antigravity conversation, the original
  database, which is most of its size.
- **A copy is kept first when one conversation is deleted,** and if the copy
  fails, the delete stops rather than going ahead. Deleting a *whole* bundle
  works the other way: a bundle can be gigabytes, and copying one in order to
  delete it would be a move nobody asked for.
- **`delete_bundle` refuses any folder without a `manifest.json`.** It is the
  only place Ferry removes a folder it did not create, and that check is what
  stands between a mistyped path and someone's Documents folder.

### Sealed bundles

A **sealed bundle** is a single `.ferry` file: the packed bundle, encrypted as
a whole with AES-256-GCM under a key made from a passphrase with scrypt.
Nothing about it can be read without that passphrase, not the number of
conversations, not which tools they came from, not when it was made.

The code is in `src/ferry/core/crypto.py` (the framing) and
`src/ferry/core/sealed.py` (what is sealed, and when).

**Why seal the whole bundle rather than encrypt each file.** Encrypting each
file as it was written would mean every read and write path learning about
keys: four adapters, `validate()`, the inspect screen, the attachment
checksums. A mistake in any of them would be *silent*, leaving files that look
encrypted and can never be opened. Sealing puts encryption in one place that
can be checked, and that either works or visibly does not.

**What that costs, which the interface also says.** The unencrypted bundle
exists on disk while it is being made, and again under `~/.ferry/open` while
Ferry reads a sealed one. Both are deleted afterwards, but on most disks the
data is not overwritten, so a recovery tool could find it until the space is
reused. **Sealing protects a bundle you carry or store. It does not protect the
machine that made it.**

**The framing adds what AES-GCM alone does not:**

- **A frame cannot be moved.** Its number is part of its nonce.
- **The end is authenticated.** Each 1 MiB frame says whether it is the last,
  and that flag is covered by the tag. Without it, cutting a file *between*
  frames would leave every remaining frame intact, and decryption would stop at
  the end of the file and hand back two thirds of a conversation as though it
  were complete.
- **A failure leaves nothing.** The output is renamed into place only once the
  last frame is authenticated. Half a decrypted file is worse than none,
  because it reads as real content.

**Rules the interface enforces:**

- The passphrase is asked for twice. There is no recovery.
- The sealed file is opened again with the same passphrase **before** the
  unencrypted bundle can be deleted, and deleting it is asked separately.
- A sealed bundle can be changed in two ways. **Unsealing to a folder** writes
  the bundle out and leaves the `.ferry` file alone. **Deleting and sealing
  again** writes the new file *beside* the old one, opens it again with the same
  passphrase to prove it can be read, and only then renames it into place, so a
  crash before that leaves the previous file untouched.
- A bundle is sealed again with the passphrase it was opened with. A different
  one would keep the file name and quietly stop opening the way it did
  yesterday.
- Ferry never stores a passphrase, and never writes one into a log or an error.

Measured on the reference machine: a 36.2 MB Antigravity bundle seals to
11.7 MB in 1.8 seconds and opens in 1.8 seconds. Turning the passphrase into a
key takes 0.26 seconds, once per bundle.

## Adapters

Every adapter implements the contract described in
[ADAPTERS.md](ADAPTERS.md):

- `detect()` never raises, and reports whether the tool is installed, its
  version, where its data is, how many conversations it has, and notes for the
  person.
- `export(dest_bundle_dir)` yields `ExportEvent`s and must be resumable.
- `import_(bundle_dir, options)` yields `ImportEvent`s and backs up by default.
- A set of optional methods describes how to take back what an import wrote.

The four real adapters are registered in `ferry/adapters/__init__.py`.

`import_()` reads UCS files from a bundle, and it has never required that those
files came from its own tool. That is what makes moving between tools possible
without new import machinery.

### `ImportOptions`, and holding every adapter to them

| Option | Meaning |
|---|---|
| `backup` | Copy anything about to be overwritten into `~/.ferry/backups` first. On by default. |
| `dry_run` | Report what would happen, and **write nothing**. |
| `on_conflict` | `skip` (the default), `rename` or `overwrite`. |
| `path_remap` | `(old_prefix, new_prefix)` pairs, tried in order; the first match wins. |
| `allow_cross_tool` | Allow a conversation from a different tool. Off by default. |
| `mode` | `archive` (the default) or `continue`, for a conversation crossing tools. |
| `only` | Conversation ids to import. Empty means all of them. |
| `trust_folders` | Let the target open the folders written into, where it needs telling. Off by default; only the menu turns it on, and only when the person agrees. |

Every adapter was tested against these in its own test file, except Copilot,
whose import ignored the options entirely for two milestones. **A dry run into
Copilot Chat would have written** a row into VS Code's chat index and a
transcript file. Nothing caught it, because the interface passed the defaults
and never set `dry_run`, so the option could only be reached from tests nobody
had written.

`tests/test_import_contract.py` now asks all four adapters the same questions,
so a fifth adapter inherits the whole contract just by being added to
`BUILDERS`.

### What `rename` means

All four tools identify a conversation by its id **and put that id in the file
name**. Writing `<uuid>-1.jsonl` while the file still says `sessionId: <uuid>`
inside produces a conversation that contradicts itself, and in Claude Code one
that shares its stored tool output with the very file it was trying not to
overwrite.

So `rename` imports a **new identity**: a fresh id in the file name, in the
records, and in anything keyed on it. Antigravity refuses instead, and says
why: its id is written through encoded records Ferry cannot safely re-identify,
and half a re-identified database is worse than no copy at all.

### Backups

`ferry.core.backup` writes one folder per Ferry run,
`~/.ferry/backups/<timestamp>/<tool>/`, with a `manifest.jsonl` recording each
file's original path. Each adapter used to have its own version of this, which
worked out the timestamp per file, so an import crossing a second boundary was
split across folders, and recorded nothing about where a file came from, which
made the copies useless for the one thing a backup is for.

Ferry never deletes backups on its own. The person removes them from *Clean up
backups*.

## Layout

```text
src/ferry/
├── cli/          the menu, commands, screens and themes
├── core/         bundles, sealing, backups, provenance
├── ucs/          the schema models
├── adapters/     one package per tool, plus taking back an import
├── compact/      turning a conversation into a document, offline
├── skill/        SKILL.md, shipped inside the package
└── config.py     saved settings
scripts/          schema generation, the logo, the self-checks
schemas/          the generated JSON schema, checked in
```

## The CLI layer

Running `ferry` on its own scans for assistants and opens a menu, so nobody
needs to learn command names first. Underneath, the same work is available as
commands for scripts and for AI assistants: `ferry tools`, `ferry export`,
`ferry import`, `ferry remove`, `ferry compact` and `ferry skill`.

```text
ferry.cli.__init__    the typer app, commands and flags
ferry.cli.commands    export, import and remove, without the menu
ferry.cli.menu        the scan screen and the menu
ferry.cli.flows       the screens behind each menu item
ferry.cli.ui          THE presentation layer: all output and prompts
ferry.cli.helpstyle   Ferry's theme and wordmark on --help
ferry.cli.theme       palettes, symbol sets, terminal detection
```

### Everything on screen goes through `ferry.cli.ui`

No module outside `ferry.cli` may import `rich` or `prompt_toolkit` directly.
The moment an adapter printed its own coloured output, half the interface would
stop following the active theme, and the fallbacks below would quietly stop
applying. `UI` is the only way to write to the terminal.

It also escapes everything it prints. `rich` reads square brackets as
formatting, and the plain text symbols are square brackets, so without that
`[ok]` and `[i]` would vanish from piped output.

### Themes and fallbacks

Four themes ship: `harbor` (the default), `compass`, `classic` and `mono`. The
theme is chosen from `--theme`, then `FERRY_THEME`, then the saved choice, then
`harbor`. An unknown name falls back to the default rather than stopping Ferry,
because a typo should never block someone moving their history.

Two independent checks then limit the result:

| Condition | Effect |
|---|---|
| Not a terminal, `NO_COLOR` set, or `TERM=dumb` | The whole theme drops to `mono` |
| The output's encoding cannot show the symbols | Symbols drop to plain text, **colours kept** |

The second check prevents a crash, not just an ugly screen. Windows consoles
often use `cp1252`, which cannot encode `✓`, and printing one there raises
`UnicodeEncodeError` and stops the program. `supports_unicode()` tries the full
symbol set against the output's encoding and swaps only the symbols, so a
Windows user still gets a coloured interface.
