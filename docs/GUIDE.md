# Using Ferry

Every screen, and what each option does. Written for the person using Ferry
rather than the person building it — for that, see
[ARCHITECTURE.md](ARCHITECTURE.md).

## The idea in one paragraph

Your conversations live inside each assistant, in that assistant's own format,
on this machine. Ferry reads them into a **bundle** — an ordinary folder of
JSON files that belongs to you rather than to any tool — and can write them
back out into any assistant it supports. Backing up, moving to a new machine,
and moving to a different assistant are all the same two steps: export to a
bundle, import from it.

A bundle is a folder you can open, copy to a USB stick, or back up like
anything else. Nothing about it is secret or locked, unless you seal it.

## Starting Ferry

```bash
ferry
```

Arrow keys move, Enter chooses, **Escape backs out** of any screen without
doing anything. Typing `/` filters the menu, so `/theme` jumps to the theme
picker. A list longer than the screen scrolls with the cursor and says how many
rows are above and below; Page Up and Page Down move a screen at a time, Home
and End jump to either end.

Ferry needs a real terminal. Run it from your own terminal window, not from a
pipe or a script — it will say so and exit cleanly rather than hanging.

The first thing it does is look for assistants and show what it found:

```
Claude Code             v2.1.237 · 2 conversations
OpenAI Codex            v0.147.0 · 6 conversations
GitHub Copilot Chat     5 conversations
Google Antigravity IDE  not found
```

**The count is what the application lists, not the files on disk.** An unused
chat panel leaves a file nobody had a conversation in, and a subagent gets a
file of its own the tool never shows you. Both are still carried into a bundle;
they are just not counted as conversations you had.

---

## Exporting

*Export conversations to a bundle.*

**1. Which assistant.** Skipped when only one was found.

**2. Where the bundle goes.** Ferry suggests a new folder in the directory you
started it from, named with today's date. Enter accepts it.

If you point at a folder that already has something in it, Ferry asks whether
to add to it. That is how an interrupted export resumes: run it again at the
same folder and it skips what is already written.

**3. It runs**, with a progress bar and any warnings after it. Warnings are not
failures — they say what could not be carried, conversation by conversation.

**4. It offers to seal the bundle.** The default is no.

Ferry never modifies the assistant it read from. An export is read-only.

### Sealing a bundle

Sealing encrypts the whole bundle into a single `.ferry` file with a passphrase
you choose. Use it if the bundle is going anywhere you do not control — a
shared drive, a cloud folder, a stick in a bag.

You type the passphrase twice. Then Ferry seals the bundle, **opens the sealed
file again with the same passphrase to prove it works**, and only then offers
to delete the unencrypted copy. That offer defaults to no.

**There is no recovery.** A passphrase you cannot remember is a bundle you
cannot open, and Ferry cannot help you — that is what encryption means. The
sealed file's contents cannot be read without it.

---

## Importing

*Import a bundle into a tool.* This is the only part of Ferry that writes into
your real conversation history, and it is the part worth reading closely.

### 1. Which bundle

Ferry lists the bundles it can find in the folder you started it from, plus
your Desktop, Downloads and Documents. *Somewhere else* lets you type a path.

Sealed bundles ask for the passphrase and are opened into a temporary folder
that exists only while the import runs.

### 2. Which assistant to import into

Every assistant found on this machine is offered.

### 3. Where the folders should be read as now

**Only asked when the bundle came from a different machine** — specifically,
when the home folder recorded in the bundle does not exist here.

A conversation remembers the project directory it happened in. On a new
machine, `/home/amr/work` may need to be read as `C:\Users\Dell\work`.

| Option | What it does |
|---|---|
| *This machine's home folder* | Rewrites the old home to yours. The usual answer. |
| *Somewhere else* | You type the folder yourself. |
| *Leave them as they are* | Paths stay exactly as recorded. Choose this if you have mirrored the old layout here. |

Only structural paths are rewritten — the working directory a conversation ran
in, and pointers to files beside it. **A path inside a message is left alone**,
because that is something you or the assistant said, and Ferry does not edit
what was said.

### 4. What to do about conversations from another assistant

**Only asked when the bundle holds conversations the target assistant did not
make.** Restoring Codex into Codex never sees this screen: a restore is not a
migration.

Ferry first says what converting would cost, counted from your actual
conversations — how many thinking blocks lose their signature, how many tool
calls become text, how many images need their bytes present.

| Option | What it does |
|---|---|
| *Import only the N that <tool> made* | Skips everything foreign. **Only offered when the bundle actually holds some** — otherwise it would import nothing. |
| *Convert them so I can read and search them here* | Keeps the most detail. This is `archive`, and it is the usual answer. |
| *Convert them so I can carry on working in them* | Gives up more on purpose: drops the assistant's thinking and tool output, so the target's assistant can continue the conversation without choking on records it cannot verify. |
| *Cancel* | Nothing is written. Not even the conversations that needed no conversion. |

**Why "carry on working" gives up more, not less.** A thinking block carries a
signature issued by the vendor whose model produced it, and no one else can
reissue it — a target that checks signatures rejects the conversation outright.
A tool call naming a tool the target does not have describes something that
cannot have happened there. Dropping both is what makes the conversation
continuable; keeping them is what makes it complete. You cannot have both, so
Ferry asks which you want.

### 5. The write screen

This screen warns you it writes into your real history, and the option under
the cursor is the one that writes nothing.

| Option | What it does |
|---|---|
| *Preview it first* | Runs the whole import without writing anything, and lists what would happen. Then asks again. |
| *Choose which conversations to import* | A checklist, everything ticked to begin with. Returns here afterwards with the count updated. |
| *Import, and keep the copy <tool> already has* | Any conversation the tool already has is left exactly as it is. |
| *Import, and keep both copies* | The one from the bundle arrives **beside** the existing one, under a new id. |
| *Import, and replace the copy <tool> already has* | The copy being replaced is saved to `~/.ferry/backups/` first. |
| *Cancel* | Nothing is written. |

The first three are safe in the sense that nothing you already have changes.
Only *replace* touches an existing conversation, and it takes a backup first.

**An import is never half-written.** A conversation that cannot be turned into
a real conversation in the target produces no file at all, and is reported with
the reason.

---

## Where your conversations appear afterwards

Every assistant has something that decides whether it will *show* you a
conversation, beyond the file being correct. If an import reported success and
the tool's list looks unchanged, this is almost always why.

**Claude Code** — a conversation lives in a project folder, and Claude Code
will not open one in a folder you have not trusted. Ferry grants that trust as
part of the import. Run `claude --resume` to see the list.

**GitHub Copilot Chat** — a conversation is invisible unless it is also listed
in the workspace chat index, which Ferry writes. The list belongs to a specific
workspace, so open the folder the conversation happened in. If VS Code was
already running when you imported, reloading the window is worth trying before
concluding anything went wrong — though whether VS Code re-reads that index on
reload has not been measured here.

**OpenAI Codex** — two things matter. Ferry writes a row into Codex's session
database, without which nothing lists the conversation. And the CLI picker
**filters by the folder you are standing in**: run `codex resume` from the
project the conversation happened in, or you will not see it. The desktop app
lists everything regardless of folder.

**Google Antigravity** — two things matter, and one of them is unusual. A
conversation belongs to a **project**, so open the folder your other
conversations are in; and Antigravity keeps its list in a file separate from the
conversations themselves, which Ferry writes as part of the import. **Close
Antigravity before importing:** it holds that list in memory and writes it back
when it exits, so anything added underneath a running app is discarded. Ferry
says so rather than reporting a success you would only discover was empty.

---

## Inspecting a bundle

*Inspect a bundle.* Read-only until you deliberately ask for a deletion.

It shows what the bundle holds — which tools, how many conversations, how many
messages, attachments and their checksums, and whether anything is wrong.

Two deletions are offered:

- **Delete one conversation from this bundle.** It is removed from the bundle,
  its attachments with it, and the manifest is updated.
- **Delete this whole bundle.** Ferry refuses to delete any directory that does
  not carry a `manifest.json`, so it cannot be pointed at a folder that is not
  a bundle.

Both ask to confirm, and both default to no. **Neither touches the assistant
the conversations came from.** Deleting from a bundle deletes your backup copy,
not your history.

A sealed bundle cannot be edited in place. Unseal it first — the screen offers
that.

---

## Deleting what Ferry imported

*Delete conversations Ferry imported.* Takes back conversations Ferry converted
into an assistant — for when you tried a migration and do not want the result.

For each assistant it lists every conversation Ferry brought in from another
one, in two groups:

- **Can be deleted** — still exactly what Ferry wrote. Ferry keeps a checksum
  of every conversion it writes, which is how it knows. Opening a conversation
  to look at it does not count. Every assistant Ferry supports changes the file
  a little when it shows a conversation — a few header bytes in Antigravity,
  lines of bookkeeping in VS Code, one marker line in Claude Code, a rewrite
  into its newer format in Codex — and Ferry proves the conversation itself is
  untouched before offering it. (A Codex conversation imported before
  11 September 2026 and opened since cannot be proven this way, and stays.)
- **Will stay** — you have opened it and carried on, so it is yours now; or it
  is no longer where Ferry put it; or it was imported before Ferry kept
  checksums, so it cannot tell. Each is listed with its reason, and none is
  offered.

**Nothing is ticked to begin with.** Tick what should go, and Ferry says how
many conversations it is about to delete from your real history and asks,
defaulting to no.

A delete removes both halves of what the import wrote: the conversation, and
the entry that makes the assistant list it — Copilot's chat index, Codex's
session database, Antigravity's conversation list. A copy of each file, and of
each list it changes, goes to `~/.ferry/backups` first.

What it will not do:

- **Delete a restore.** Importing your own conversation back into the assistant
  it came from puts back *your* history. Ferry keeps no record of that, and
  never offers it.
- **Delete while VS Code or Antigravity is open.** Both keep their list in
  memory and write it back when they close, which would put the entry straight
  back. The screen says so before you choose anything.
- **Take back a folder Claude Code was allowed to open.** That setting is
  shared with your own conversations in the same folder, so it stays.
- **Touch the assistant a conversation originally came from.** It only removes
  what Ferry wrote.

If a delete stops partway — the list was locked, say — run it again. It
finishes the job rather than starting over.

---

## Cleaning up backups

*Clean up backups.* Before an import replaces anything, and before a delete
removes anything, Ferry copies it into `~/.ferry/backups/` — one folder per run,
named by the time it was taken, with a `manifest.jsonl` recording where each
copy came from so it can be put back. **Ferry never removes these by itself.**
This screen is where you do.

It opens by saying how many backups there are and how much space they take,
then offers two things:

- **Delete the backups from temporary folders.** Offered only when there are
  some: backups holding nothing but copies of files from a temporary or test
  folder, which were never part of your history. Earlier versions of Ferry's
  own test suite left many of these.
- **Choose which backups to delete.** Every backup, newest first, with its date,
  its size and what it holds — which assistant, *a bundle*, *Claude Code
  settings*, or *temporary folders*. The newest backup for each assistant is
  marked. **Nothing is ticked to begin with.**

Either way it says how many backups it is about to delete and how much that
frees, names any assistant whose newest backup is among them, and asks,
defaulting to no. **A deleted backup cannot be put back** — it was the copy.

It only ever touches folders Ferry made: a folder in `~/.ferry/backups` named
anything other than a Ferry timestamp is never listed and never deleted.

---

## Compacting a conversation

*Compact a conversation into a summary.* Turns one conversation into a markdown
document you can paste as the first message of a new session, when you want to
carry the work rather than the whole transcript.

Also available as a command, so it pipes:

```bash
ferry compact <bundle> --conversation <id> --shape handoff --length standard
```

Omit `--conversation` and it lists what the bundle holds. `--out <file>` writes
to a file instead of standard output.

| Setting | Choices |
|---|---|
| `--shape` | `handoff` (what someone needs to continue), `said` (what was said), `done` (what was done) |
| `--length` | `brief`, `standard`, `full` |

**It runs entirely on this machine.** No API key, no account, no model, no
network. It invents nothing: every line is either quoted from your conversation
word for word or counted from it. What it gives up in exchange is narrative —
it can quote you the three facts, but not write the sentence that joins them.

---

## Appearance

```bash
ferry --theme compass    # harbor, compass, classic, or mono
ferry --no-color         # plain ASCII markers, no colour
ferry --verbose          # more detail about what Ferry is doing
```

*Change theme* in the menu previews each one and remembers your choice.

Ferry degrades rather than breaking: on a terminal that cannot render its
icons, it uses ASCII ones and keeps the colour.

---

## Where Ferry keeps its own files

| Path | What it holds |
|---|---|
| `~/.ferry/backups/` | Copies taken before an import replaced anything or a delete removed anything. Ferry never deletes these by itself; *Clean up backups* removes the ones you choose. |
| `~/.ferry/provenance/` | One small record per converted conversation: its title, where it came from, what the conversion cost, and a checksum of what Ferry wrote. |

The provenance record is why an exported conversation still knows it was
converted, and why *Delete conversations Ferry imported* can tell a
conversation Ferry wrote from one you have since worked in. Deleting a
conversation removes its record too.

---

## Two honest limits

**Ferry never modifies your source data.** Every export is read-only, and this
is checked on real data before every release, not assumed.

**Ferry makes no network calls at all.** Nothing it reads leaves this machine.

## When something is not there

Start with [TROUBLESHOOTING.md](TROUBLESHOOTING.md). The single most common
cause is the section above — the file is written correctly and the tool has not
been told to list it, or you are looking from the wrong folder.

## A note on flags

Ferry's menu is the interface. When a prompt cannot be shown because there is
no terminal, the error suggests a command-line flag — and **most of those flags
do not exist yet.** Today `ferry` takes `--theme`, `--no-color`, `--verbose`
and `--version`, plus the `tools` and `compact` commands. Treat the suggestions
in those messages as a description of what is planned, not of what you can run.
