# Troubleshooting

Things that go wrong, why, and what to do. Ordered roughly by how often they
come up.

If something here does not match what you are seeing, that is worth reporting —
a wrong answer in this file is worse than a missing one.

## Ferry says a tool is not installed, but it is

Ferry looks for the tool's **conversation store**, not the application. A tool
installed but never used has no store yet, and Ferry will say it is not there.

Open the tool, have one conversation, and scan again.

If you have used it and Ferry still cannot see it, the store is somewhere Ferry
does not expect — a portable install, a non-default data directory, a
`--user-data-dir` flag, or a VS Code fork that is not Antigravity. Ferry reads
each tool's documented location; it does not search your disk.

## The count does not match what the application shows

This is usually Ferry being right, and it will tell you why in the line under
the count.

| What you see | What it means |
|---|---|
| *N empty* | The tool wrote a file when a chat panel opened and you never typed anything. VS Code does this constantly. Not a conversation, not exported. |
| *N not shown by the app* | A subagent thread. Codex and Antigravity give a spawned agent its own file, and the application never lists it. Ferry counts it separately rather than inflating the total. |
| *N duplicates* | Two files claiming the same conversation id. Counted once. |

Ferry counts **conversations**, not files. Every adapter used to count files,
and every adapter was wrong in at least one of these four ways.

## "There is nothing at that path" / "is not a bundle"

The path prompt tells you which of four things went wrong:

- **There is nothing at `<path>`** — the path does not exist. Check for a typo;
  what you typed is still in the line, so you can fix the character.
- **`<name>` is not a bundle — there is no manifest.json in it** — that folder
  is not a Ferry bundle. If your bundles live *inside* it, Ferry will offer
  them instead of complaining.
- **`<name>` is a file, not a bundle** — you pointed at a document. A bundle is
  a folder, or a sealed `.ferry` file.
- **named like a sealed bundle but does not begin like one** — the file is
  called `.ferry` but is not one. Either it was renamed, or it is truncated or
  damaged.

Pasting a path from Windows Explorer's **Copy as path** works — the quotes it
adds are stripped.

Escape leaves the prompt at any point. So does pressing enter on an empty line.

## A sealed bundle will not open

The message says the passphrase may be wrong **or** the file may have been
altered, and that Ferry cannot tell those apart. That is literally true, not a
hedge: the authentication check fails identically for both, by design.

Things worth checking, in order:

1. **The passphrase.** You get three tries per attempt; escape leaves
   immediately and does not spend one.
2. **The file transfer.** A bundle sent over a channel that "helpfully"
   converts line endings, or truncated by a failed copy, will not open. Compare
   the file size with the original.
3. **The file itself.** A single flipped bit anywhere in a sealed bundle makes
   it unopenable. This is the point of sealing, not a defect — but it means a
   sealed bundle on failing storage is a sealed bundle you may lose.

**There is no recovery without the passphrase.** Not by Ferry, not by anyone.
No reset, no hint, nowhere it is stored.

## I imported, but the conversations are not in the application

Three causes, most common first.

**The application was running.** Close the target tool completely before
importing. Every one of the four caches its conversation list in memory and
writes it back on exit, which can overwrite what Ferry just wrote.

**Copilot Chat needs its index.** A conversation file alone is invisible to VS
Code; the workspace's chat index has to list it too. Ferry writes both. If you
copied files by hand instead, that is the missing half.

**The conversation belongs to a workspace that is not open.** Copilot and
Antigravity file conversations under the project they happened in. Open that
folder and look again.

## The conversation is there but every path in it is wrong

A conversation records the absolute path of the directory it happened in, and
that path is usually wrong on a new machine — a different username, a different
drive.

When a bundle records a home folder that is not on this machine, Ferry asks
where those folders live now, before writing anything. If you skipped that
prompt, re-import and answer it.

## Something was overwritten and I want it back

Ferry copies a file into `~/.ferry/backups/<timestamp>/<tool>/` before
overwriting it, and records where it came from in `manifest.jsonl` beside it.
That file is one line per copy, in plain JSON:

```
{"tool": "...", "original": "...", "stored": "...", "backed_up_at": "..."}
```

Copy the file back to the path in `original`, with the tool closed.

Backups are per Ferry run, not per file, so one import produces one folder you
can read.

**Deleting a whole bundle does not back it up**, and the screen says so before
it does it. A bundle *is* the backup — there is nowhere for a copy to go.

## Deleting a conversation from a bundle did not free the space I expected

It freed more, most likely. A conversation is not one file: the UCS document,
its `attachments/<id>/` folder, and `source_raw/<id>.bin` — the original file
Ferry carried across, which for an Antigravity conversation is a whole SQLite
database and most of its size.

The screen names the file count and the megabytes before asking.

## An unsealed folder is sitting on my disk

Unsealing writes a plain, unencrypted copy of every conversation in the bundle.
Ferry warns before doing it and leaves the folder where you asked for it —
deleting it when you are done is yours to do.

Note that deleting it removes the directory entry, not the blocks. On most
filesystems a forensic tool could recover them until that space is reused.
**Sealing protects a bundle you carry or store; it does not protect the machine
that made it.** If you need the second thing, you need full-disk encryption.

## Ferry is slow on a large store

Two operations dominate, and both are honest work:

- **Sealing and opening.** Key derivation is deliberately expensive — about a
  quarter of a second — and is what makes a weak passphrase costly to attack.
  It happens once per bundle, not per file.
- **Reading Antigravity.** Conversations are protobuf blobs inside SQLite
  databases, and Ferry parses every one.

A 140 MB bundle across four tools seals in about five seconds on a normal
laptop.

## Windows: a path is too long

Windows refuses paths over 260 characters unless long paths are enabled. Deep
workspace-storage folders plus a long bundle name can cross it.

Put the bundle somewhere short — `C:\bundles\` rather than a nested Downloads
folder — or enable long path support in Windows.

## Nothing here matches

Run the self-check for the milestone that covers what you are doing:

```bash
make verify-m7
```

It prints a numbered PASS/FAIL line per property, runs against the real data on
your machine, never prints conversation content, and never modifies anything.
The failing line is the useful thing to report.
