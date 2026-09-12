# Using Ferry

Every screen and every command, and what each one does. This page is for the
person using Ferry. If you want to know how it is built, read
[ARCHITECTURE.md](ARCHITECTURE.md).

## The idea in one paragraph

Your conversations live inside each assistant, in that assistant's own format,
on your computer. Ferry reads them into a **bundle**, an ordinary folder of
JSON files that belongs to you rather than to any tool, and can write them back
into any assistant it supports. Backing up, moving to a new machine and moving
to a different assistant are all the same two steps: export to a bundle, then
import from it.

A bundle is a folder you can open, copy to a USB stick, or back up like
anything else. Nothing about it is secret or locked unless you seal it.

## Starting Ferry

```bash
ferry
```

Arrow keys move, Enter chooses, and **Escape backs out** of any screen without
doing anything. Typing `/` filters the menu, so `/theme` jumps straight to the
theme picker. A list longer than the screen scrolls with the cursor and shows
how many rows are above and below. Page Up and Page Down move a screen at a
time, and Home and End jump to either end.

The menu needs a real terminal. Run from a pipe or a script, it says so and
stops rather than waiting forever. For scripts, use the commands described in
[From a script](#from-a-script).

The first thing Ferry does is look for your assistants and show what it found:

```text
  ✓  Claude Code          2.1.266 · 4 conversations
  ✓  OpenAI Codex         0.146.0-alpha.3.1 · 5 conversations
  ✓  GitHub Copilot Chat  1.136.2 · 5 conversations
  ·  Antigravity          not found
```

**The count is what each application lists, not the number of files on disk.**
An unused chat panel leaves a file nobody had a conversation in, and a subagent
gets a file of its own that the tool never shows you. Both are still carried
into a bundle. They are just not counted as conversations you had.

## Exporting

*Export conversations to a bundle.*

1. **Which assistant.** Skipped when only one was found.
2. **Where the bundle goes.** Ferry suggests a new folder, named with today's
   date, in the directory you started it from. Enter accepts it.
3. **It runs,** with a progress bar, and any warnings are listed afterwards.
   Warnings are not failures. They say what could not be carried, conversation
   by conversation.
4. **It offers to seal the bundle.** The answer under the cursor is no.

The folder you name is the bundle itself, so it holds nothing but Ferry's own
files. Two things follow from that.

If you name a folder that is already a bundle, Ferry asks whether to add to
it. That is how an interrupted export carries on: run it again into the same
folder and it skips whatever is already written.

If you name a folder with your own files in it, such as Downloads, Ferry says
it cannot be the bundle and offers to make a new folder inside it instead,
named with today's date. Enter accepts that, and the bundle lands in
Downloads without disturbing anything else there.

An export only reads. Ferry never changes the assistant it reads from.

### Sealing a bundle

Sealing encrypts the whole bundle into a single `.ferry` file, locked with a
passphrase you choose. It is worth doing whenever the bundle is going
somewhere you do not control: a shared drive, a cloud folder, a USB stick in a
bag.

You type the passphrase twice. Ferry seals the bundle, **opens the sealed file
again with the same passphrase to prove it works**, and only then offers to
delete the unencrypted copy. That offer starts on no.

**There is no recovery.** If you forget the passphrase, nobody can open the
bundle, Ferry included. That is what encryption means.

## Importing

*Import a bundle into a tool.* This is the only part of Ferry that writes into
your real conversation history, so it is the part worth reading closely.

### 1. Which bundle

Ferry lists the bundles it can find in the folder you started it from and in
your Desktop, Downloads and Documents folders. *Somewhere else* lets you type
or paste a path.

A sealed bundle asks for its passphrase and is opened into a temporary folder
that exists only while the import runs.

### 2. Which assistant to import into

Every assistant found on your computer is offered.

### 3. Where the folders are now

**Only asked when the bundle came from a different machine,** meaning the home
folder recorded in the bundle does not exist on this one.

A conversation remembers the project folder it happened in. On a new machine,
`/home/amr/work` may need to be read as `C:\Users\Dell\work`.

| Option | What it does |
|---|---|
| *This machine's home folder* | Reads the old home folder as yours. The usual answer. |
| *Somewhere else* | You type the folder yourself. |
| *Leave them as they are* | Paths stay exactly as recorded. Choose this if you have recreated the old layout here. |

Only structural paths change: the folder a conversation ran in, and pointers to
files stored beside it. **A path mentioned inside a message is left alone,**
because that is something you or the assistant said, and Ferry does not edit
what was said.

### 4. Conversations from another assistant

**Only asked when the bundle holds conversations that the assistant you are
importing into did not make.** Restoring Codex into Codex never sees this
screen, because a restore is not a conversion.

Ferry first tells you what converting would cost, counted from your actual
conversations: how many reasoning blocks lose their signature, how many tool
calls become plain text, and how many images need their files in the bundle.

| Option | What it does |
|---|---|
| *Import only the N that <tool> made, and skip the rest* | Leaves the foreign ones out. Offered only when the bundle holds some of the tool's own conversations. |
| *Convert them so I can read and search them here* | Keeps the most detail. The usual choice. |
| *Convert them so I can carry on working in them* | Gives up more on purpose: it drops the assistant's private reasoning and the output of its tool calls, so the new assistant can continue the conversation. |
| *Cancel* | Nothing is written, not even the conversations that needed no conversion. |

**Why carrying on means giving up more.** The assistant's private reasoning is
signed by the company whose model produced it, and nobody else can issue that
signature, so an assistant that checks it rejects the conversation. A tool call
naming a tool the new assistant does not have describes something that could
not have happened there. Leaving both out is what makes the conversation
possible to continue. Keeping both is what makes it complete. You cannot have
both, so Ferry asks.

Nothing is written on this screen. The next one decides that.

### 5. The write screen

This screen warns you that it writes into your real history, and the option
under the cursor is the one that writes nothing.

| Option | What it does |
|---|---|
| *Preview it first* | Runs the whole import without writing anything and shows what would happen. Then asks again. |
| *Choose which conversations to import* | A checklist with everything ticked to begin with. Comes back here with the count updated. |
| *Import, and keep the copy <tool> already has* | A conversation the tool already has is left exactly as it is. |
| *Import, and keep both copies* | The one from the bundle arrives **beside** the existing one, under a new id. |
| *Import, and replace the copy <tool> already has* | The copy being replaced is saved to `~/.ferry/backups` first. |
| *Cancel* | Nothing is written. |

Only *replace* touches a conversation you already have, and it takes a backup
first.

**An import is never half written.** A conversation that cannot become a real
conversation in the target produces no file at all, and Ferry tells you why.

## Where your conversations appear afterwards

Every assistant has something beyond the file itself that decides whether it
*shows* you a conversation. If an import reported success and the tool's list
looks unchanged, this is almost always the reason.

**Claude Code.** A conversation lives in a project folder, and Claude Code will
not open one in a folder it has not been told to trust. When you import from
the menu, Ferry names any such folders and offers to add them for you, saving
the current settings first. A command import never changes those settings: it
names the folders, and you start Claude Code once in each. Run `claude --resume`
to see the list.

**GitHub Copilot Chat.** A conversation only appears if it is also listed in
the workspace's chat index, which Ferry writes. That list belongs to one
workspace, so open the folder the conversation happened in. **Close VS Code
before importing:** it keeps the list in memory and writes it back when it
closes.

**OpenAI Codex.** Ferry writes a row into Codex's session database, without
which nothing lists the conversation. The command line picker also **only
shows sessions for the folder you are in**, so run `codex resume` from the
project the conversation happened in. The desktop app lists everything.

**Google Antigravity.** A conversation belongs to a project, so open the folder
your other conversations are in. Antigravity also keeps its list in a file of
its own, which Ferry writes as part of the import. **Close Antigravity before
importing:** it holds that list in memory and writes it back when it exits,
which would throw away anything added while it was running. Ferry checks, and
tells you rather than reporting a success you would later find was empty.

## Inspecting a bundle

*Inspect a bundle.* Nothing changes unless you ask it to delete something.

It shows what the bundle holds: which tools, how many conversations and
messages, the attachments and their checksums, and anything wrong with it.

Two deletions are offered:

- **Delete one conversation from this bundle.** It goes, with its
  attachments, and the bundle's record of what it holds is updated.
- **Delete this whole bundle.** Ferry refuses any folder without a
  `manifest.json`, so it cannot be pointed at a folder that is not a bundle.

Both ask first, and both start on no. **Neither touches the assistant the
conversations came from.** Deleting from a bundle deletes your backup copy, not
your history.

A sealed bundle cannot be edited in place. The screen offers to unseal it
first.

## Deleting what Ferry imported

*Delete conversations Ferry imported.* Takes back conversations Ferry converted
into an assistant, for when you tried a migration and do not want the result.

For each assistant it lists every conversation Ferry brought in from another
one, in two groups.

**Can be deleted:** still exactly what Ferry wrote. Ferry keeps a checksum of
every conversion it writes, which is how it knows. Opening a conversation to
look at it does not count as changing it. Every assistant changes the file a
little when it shows a conversation (a few header bytes in Antigravity, some
bookkeeping in VS Code, one marker line in Claude Code, a rewrite into its
newer format in Codex), and Ferry proves the conversation itself is untouched
before it offers it. A Codex conversation imported before 11 September 2026 and
opened since cannot be proven this way, so it stays.

**Will stay:** you have opened it and carried on, so it is yours now; or it is
no longer where Ferry put it; or it was imported before Ferry kept checksums,
so Ferry cannot tell. Each is listed with its reason.

**Nothing is ticked to begin with.** Tick what should go, and Ferry tells you
how many conversations it is about to delete from your real history and asks,
starting on no.

A delete removes both halves of what the import wrote: the conversation, and
the entry that makes the assistant list it, whether that is Copilot's chat
index, Codex's session database or Antigravity's conversation list. A copy of
each file, and of each list it changes, goes to `~/.ferry/backups` first.

What it will not do:

- **Delete a restore.** Importing your own conversation back into the assistant
  it came from puts back *your* history. Ferry keeps no record of that and
  never offers it.
- **Delete while VS Code or Antigravity is open.** Both keep their list in
  memory and would put the entry straight back when they close. The screen
  tells you before you choose anything.
- **Take away a folder Claude Code was allowed to open.** That permission is
  shared with your own conversations in the same folder, so it stays.
- **Touch the assistant a conversation originally came from.**

If a delete stops partway, for example because a list was locked, run it
again. It finishes the job rather than starting over.

`ferry remove` does the same from a script. See [From a script](#from-a-script).

## Cleaning up backups

*Clean up backups.* Before an import replaces anything, and before a delete
removes anything, Ferry copies it into `~/.ferry/backups`: one folder per run,
named by the time it was taken, with a `manifest.jsonl` recording where each
copy came from so it can be put back. **Ferry never removes these on its
own.** This screen is where you do.

It starts by saying how many backups there are and how much space they take,
then offers two things:

- **Delete the backups from temporary folders.** Offered only when there are
  some: backups holding nothing but copies of files from temporary or test
  folders, which were never part of your history. Earlier versions of Ferry's
  own test suite left a lot of these.
- **Choose which backups to delete.** Every backup, newest first, with its
  date, its size and what it holds: an assistant, *a bundle*, *Claude Code
  settings* or *temporary folders*. The newest backup for each assistant is
  marked. **Nothing is ticked to begin with.**

Either way, Ferry says how many backups it is about to delete and how much
space that frees, names any assistant whose newest backup is among them, and
asks, starting on no. **A deleted backup cannot be brought back.** It was the
copy.

It only ever touches folders Ferry made. A folder in `~/.ferry/backups` named
anything other than a Ferry timestamp is never listed and never deleted.

## Compacting a conversation

*Compact a conversation into a summary.* Turns one conversation into a short
markdown document you can paste as the first message of a new session, for when
you want to carry the work rather than the whole transcript.

It is also a command, so its output can be piped:

```bash
ferry compact <bundle> --conversation <id> --shape handoff --length standard
```

Leave out `--conversation` and it lists the conversations in the bundle, with
their ids. `--out <file>` writes to a file instead of the screen.

| Setting | Choices |
|---|---|
| `--shape` | `handoff` (what someone needs to carry on), `said` (what was said), `done` (what was done) |
| `--length` | `brief`, `standard`, `full` |

**It runs entirely on your computer.** No API key, no account, no model and no
network. It invents nothing either: every line is quoted from your conversation
word for word or counted from it. What it gives up in exchange is narrative. It
can quote you the three facts, but it cannot write the sentence that joins
them.

The command reads unsealed bundles only. For a sealed one, use the menu, which
asks for the passphrase.

## Appearance

```bash
ferry --theme compass
```

```bash
ferry --no-color
```

The themes are `harbor` (the default), `compass`, `classic` and `mono`.
`--no-color` switches colour off and uses plain text markers. `--verbose` shows
more detail about what Ferry is doing. *Change theme* in the menu previews each
theme and remembers your choice.

Ferry adapts rather than breaking. On a terminal that cannot draw its symbols,
it uses plain text ones and keeps the colour. When its output goes to a file or
another program, it drops colour altogether.

## Where Ferry keeps its own files

| Path | What it holds |
|---|---|
| `~/.ferry/backups/` | Copies taken before an import replaced anything or a delete removed anything. Ferry never deletes these on its own; *Clean up backups* removes the ones you choose. |
| `~/.ferry/provenance/` | One small record per converted conversation: its title, where it came from, what the conversion cost, and a checksum of what Ferry wrote. |

The provenance record is why a conversation still knows it was converted when
you export it again, and why *Delete conversations Ferry imported* can tell a
conversation Ferry wrote from one you have worked in since. Deleting a
conversation removes its record too.

## Two promises

**Ferry never changes your original history.** Every export only reads, and
this is checked on real data before every release, not assumed.

**Ferry makes no network calls.** Nothing it reads leaves your computer.

## When something is not there

Start with [TROUBLESHOOTING.md](TROUBLESHOOTING.md). The most common cause is
covered above: the file is written correctly, but the tool has not been told to
list it, or you are looking from the wrong folder.

## From a script

These commands do what the menu does without asking anything, so they run in a
script, in a scheduled task, or when an AI assistant drives Ferry for you.

```bash
ferry export --tool claude-code --output ~/ferry-backup
```

```bash
ferry import --bundle ~/ferry-backup --tool claude-code --dry-run
```

```bash
ferry import --bundle ~/ferry-backup --tool claude-code
```

The tool names are `claude-code`, `codex`, `copilot` and `antigravity`.
`ferry tools` shows which of them are on your computer.

**`ferry export`**

| Flag | What it does |
|---|---|
| `--tool`, `-t` | The assistant to export from. Required. |
| `--output`, `-o` | The folder to write. Without it, Ferry makes a new `ferry-bundle-<time>` folder where you are. |
| `--force` | Carry on with a folder that is already a bundle, which is how an interrupted export resumes. With `--encrypt`, it also replaces an existing sealed file. |
| `--encrypt` | Also seal the bundle into one encrypted `.ferry` file. |
| `--replace` | With `--encrypt`: delete the unencrypted folder once the sealed file has been opened again and proven to work. |
| `--passphrase` | The passphrase to seal with. See below. |

**`ferry import`**

| Flag | What it does |
|---|---|
| `--bundle`, `-b` | The bundle: a folder, or a sealed `.ferry` file. Required. |
| `--tool`, `-t` | The assistant to import into. Required. |
| `--dry-run` | Show everything that would happen. Nothing is written. |
| `--on-conflict` | When the assistant already has a conversation: `skip` keeps its copy (the default), `rename` keeps both, and `overwrite` replaces its copy after backing it up. |
| `--conversation`, `-c` | Import only this conversation, by id. Repeat it for more. |
| `--path-remap OLD=NEW` | Read folders recorded under `OLD` as being under `NEW`, for a bundle made under a different home folder. Repeatable. |
| `--allow-cross-tool` | Convert conversations that came from a different assistant. Without it they are refused. |
| `--mode` | With `--allow-cross-tool`: `archive` (the default) keeps the most detail, for reading; `continue` drops reasoning and tool output so you can carry on. |
| `--passphrase` | The passphrase of a sealed bundle. See below. |

**`ferry remove`**

| Flag | What it does |
|---|---|
| `--tool`, `-t` | The assistant to delete from. Required. |
| `--conversation`, `-c` | Delete this conversation, by id. Repeatable. |
| `--all` | Every conversation Ferry imported there that can go. |
| `--dry-run` | Show what would be deleted. Nothing is. |

It deletes only what *Delete conversations Ferry imported* would: conversations
Ferry converted, unchanged since, each backed up first, and never while the
app is open. Name nothing and it lists them, with the ids to pass, and deletes
nothing.

**`ferry compact`** is described in [Compacting a conversation](#compacting-a-conversation),
and **`ferry tools`** lists the assistants on your computer.

The commands take the same safe answers the menu starts on. An import **backs
up before it writes**, and there is no flag to stop that. It **keeps a
conversation the assistant already has**, and it **never converts between
assistants** unless you say so. It also never adds a folder to Claude Code's
list of folders it may open, because that changes another program's security
settings, so only the menu offers it. A command import names those folders
instead, and you start Claude Code once in each.

**Passphrases.** Setting `FERRY_PASSPHRASE` is safer than passing
`--passphrase`, because a flag is kept in your shell history. In a terminal,
leave both out and Ferry asks.

**Exit codes.** `0` when everything worked. `1` when the work ran and part of
it failed, or the assistant was not found. `2` when the command could not
start, for example because a flag was wrong or missing, and nothing was
written. `ferry remove` and `ferry compact` also return `2` when they list what
is there instead of acting.

### Letting an AI assistant drive Ferry

Ferry ships with a `SKILL.md`: one page that teaches an AI coding assistant
when to use Ferry and exactly how, including the safety rules above. With it,
you can ask in plain words, *"move my Claude Code chats to my new laptop"*, and
the assistant runs the commands, checking with you before anything that writes
or deletes.

```bash
ferry skill --install
```

That installs it for every assistant Ferry finds on your computer, each in the
folder its own documentation names. To install for one assistant only, name it
with `--tool`:

| Command | Puts the skill in | Read by |
|---|---|---|
| `ferry skill --install --tool claude-code` | `~/.claude/skills/ferry/` | Claude Code, and Copilot |
| `ferry skill --install --tool codex` | `~/.agents/skills/ferry/` | OpenAI Codex, and Copilot |
| `ferry skill --install --tool antigravity` | `~/.gemini/config/skills/ferry/` | Google Antigravity |
| `ferry skill --install --tool copilot` | `~/.copilot/skills/ferry/` | GitHub Copilot Chat |
| `ferry skill --install --tool all` | Each of these, for every assistant Ferry finds | All of them |

GitHub Copilot in VS Code reads the Claude Code and Codex folders as well as
its own. So when Ferry installs for either of those, it gives Copilot no copy
of its own, which would only risk Ferry being listed twice.

Start a new session in the assistant and the skill is there. In Claude Code and
Copilot, `/ferry` calls it directly. A copy that is already up to date is left
alone. An older or edited copy is kept unless you add `--force`, and is backed
up before it is replaced.

For any other assistant, `ferry skill` prints the file. Point the assistant at
it, or paste it into the chat. The same file is `SKILL.md` at the root of
Ferry's repository.

Among other things, the skill tells an assistant never to import into an app
that is open, never to choose a passphrase for you, and to delete only what you
confirm.

When the menu cannot show a question because there is no terminal, its message
names the flag that answers it. Every one of those belongs to a command above,
except `--into` on the inspect screen, which has no command yet.
