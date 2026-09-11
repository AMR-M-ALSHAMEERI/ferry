# Troubleshooting

Things that go wrong, why they happen, and what to do about them, roughly in
order of how often they come up.

If something here does not match what you are seeing, please report it. A wrong
answer in this file is worse than a missing one.

## Ferry says a tool is not installed, but it is

Ferry looks for the tool's **conversation store**, not for the application
itself. A tool that is installed but has never been used has no store yet, so
Ferry reports it as not found.

Open the tool, have one conversation, and run `ferry tools` again.

If you have used it and Ferry still cannot see it, the store is somewhere Ferry
does not expect: a portable install, a non-default data folder, a
`--user-data-dir` flag, or a VS Code fork other than Antigravity. Ferry reads
each tool's usual location, or the one its environment variable points to. It
does not search your disk.

## The count does not match what the application shows

This is usually Ferry being right, and it explains why in the line under the
count.

| What you see | What it means |
|---|---|
| *N empty* | The tool wrote a file when a chat panel opened and nothing was ever typed. VS Code does this often. It is not a conversation and it is not exported. |
| *N not shown by the app* | A subagent. Codex and Antigravity give an agent they start their own file, and the application never lists it. Ferry counts it separately rather than inflating the total. |
| *N duplicates* | Two files claiming the same conversation. Counted once. |

Ferry counts **conversations**, not files. Counting files was wrong in at least
one of these three ways for every tool.

## "There is nothing at that path" or "is not a bundle"

The path prompt tells you which of four things went wrong:

- **There is nothing at `<path>`.** The path does not exist. Check for a typo.
  What you typed stays in the line, so you only need to fix the character.
- **`<name>` is not a bundle, there is no manifest.json in it.** That folder is
  not a Ferry bundle. If your bundles are *inside* it, Ferry offers them
  instead.
- **`<name>` is a file, not a bundle.** A bundle is a folder, or a sealed
  `.ferry` file.
- **Named like a sealed bundle but does not begin like one.** The file ends in
  `.ferry` but is not one. It was renamed, or it is truncated or damaged.

A path pasted from Windows Explorer's **Copy as path** works: Ferry removes the
quotes it adds.

Escape leaves the prompt at any point, and so does pressing Enter on an empty
line.

## A sealed bundle will not open

The message says the passphrase may be wrong **or** the file may have been
altered, and that Ferry cannot tell the two apart. That is literally true: the
check that fails is the same for both, by design.

Worth checking, in this order:

1. **The passphrase.** The menu gives you three tries, and Escape leaves
   without using one up. A command tries the passphrase it was given once.
2. **The copy.** A bundle sent through something that changes line endings, or
   cut short by a failed copy, will not open. Compare its size with the
   original.
3. **The file itself.** A single changed bit anywhere in a sealed bundle makes
   it impossible to open. That is the point of sealing, but it also means a
   sealed bundle on failing storage is one you can lose.

**There is no recovery without the passphrase.** Not by Ferry, not by anyone.
There is no reset, no hint, and nowhere it is stored.

## I imported, but the conversations are not in the application

The most common causes, in order:

**The application was open.** VS Code and Antigravity keep their conversation
list in memory and write it back when they close, which can undo what Ferry
just wrote. Close the tool completely, then import again.

**Copilot Chat needs its index.** A conversation file on its own is invisible
to VS Code: the workspace's chat index has to list it too. Ferry writes both.
If you copied files by hand, that is the missing half.

**The conversation belongs to a folder that is not open.** Copilot and
Antigravity file conversations under the project they happened in. Open that
folder and look again.

**Codex only lists the folder you are in.** Run `codex resume` from the project
folder the conversation happened in. The Codex desktop app lists everything.

## Claude Code lists the conversation but will not open it

Claude Code only opens conversations in folders it has been told to trust. An
import from the menu offers to add new folders for you. An import from a
command never changes that setting and names the folders instead. Start Claude
Code once in each of those folders, accept the trust question, and the
conversation opens.

## The conversation is there, but every path in it is wrong

A conversation records the full path of the folder it happened in, and that
path is usually different on a new machine: another username, another drive.

When a bundle records a home folder that is not on this machine, the menu asks
where those folders are now, before writing anything, and a command prints the
exact `--path-remap OLD=NEW` to add. If you skipped that, import again and
answer it.

## Something was overwritten and I want it back

Before replacing a file, Ferry copies it into
`~/.ferry/backups/<timestamp>/<tool>/` and records where it came from in
`manifest.jsonl` beside it. That file has one line per copy, in plain JSON:

```text
{"tool": "...", "original": "...", "stored": "...", "backed_up_at": "..."}
```

Close the tool, then copy the file back to the path in `original`.

Backups are grouped by Ferry run, not by file, so one import produces one
folder you can read.

**Deleting a whole bundle does not back it up,** and the screen says so first.
A bundle *is* the backup, so there is nowhere for a copy to go.

## Deleting a conversation from a bundle freed more space than expected

A conversation is not one file. It is the conversation document, its
`attachments/<id>/` folder, and `source_raw/<id>.bin`, the original file Ferry
carried across. For an Antigravity conversation that original is a whole
database and most of the size.

The screen shows the number of files and the megabytes before it asks.

## An unsealed folder is sitting on my disk

Unsealing writes a plain, unencrypted copy of every conversation in the bundle.
Ferry warns you first and leaves the folder where you asked. Deleting it when
you are done is up to you.

Deleting a file removes its entry, not the data on the disk. On most disks a
recovery tool could find it again until that space is reused. **Sealing
protects a bundle you carry or store. It does not protect the machine that made
it.** For that, use full disk encryption.

## Ferry is slow with a large store

Two things take real time, and both are doing honest work:

- **Sealing and opening.** Turning a passphrase into a key is deliberately
  slow, about a quarter of a second, because that is what makes a weak
  passphrase expensive to guess. It happens once per bundle, not once per file.
- **Reading Antigravity.** Its conversations are stored as encoded records
  inside databases, and Ferry decodes every one.

A 140 MB bundle across four tools seals in about five seconds on an ordinary
laptop.

## Windows: a path is too long

Windows refuses paths longer than 260 characters unless long paths are turned
on. VS Code's deep storage folders plus a long bundle name can go over.

Keep the bundle somewhere short, such as `C:\bundles\` rather than a folder
several levels inside Downloads, or turn on long path support in Windows.

## Nothing here matches

Run this and include its output when you report the problem:

```bash
ferry tools
```

It lists each assistant Ferry can see, with its version and conversation
count, and never prints anything from your conversations.

If you are working from a copy of Ferry's source, the self-checks go further.
`make verify-m3` through `make verify-m8` each test one area against the real
data on your computer, print one PASS or FAIL line per check, never print
conversation content, and never change anything.
