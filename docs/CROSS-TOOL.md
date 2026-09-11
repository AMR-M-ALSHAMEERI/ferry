# Moving a conversation between assistants

Ferry can write a conversation from one assistant into another one's history.
This page explains what works and what it costs, because both answers are more
specific than "yes".

## What you get

**A conversation you can open, search and keep working in.**

That was not always the claim. This page used to say a converted conversation
was a readable transcript and nothing more, because that was all anyone had
measured. It has since been tested in Claude Code, Copilot Chat and Codex: in
each one, somebody typed into a converted conversation and the assistant
answered from the history it had been given.

**What does not come with it is the session.** The tool calls in a converted
conversation were made by a different assistant, with a different set of
tools. Ferry carries them as readable text and never presents them as calls
the new assistant can run again. Nothing reconnects to a shell, a file watcher
or a server that was running somewhere else.

So if you want your history kept, searchable and alive in a new tool,
converting does that. If you would rather start a fresh session with a short
summary than bring the whole history, [Compact](GUIDE.md#compacting-a-conversation)
is the better fit.

## What works

This was measured on real conversations, not assumed. Each combination was
imported into a scratch copy of the tool and then **exported back out again**,
because "the import said it worked" and "the conversation is really there" are
different claims.

| Into | Works | How |
|---|---|---|
| **Claude Code** | Yes | Its transcripts are plain text records, and everything around a message can be rebuilt. Every message and every word from all three other tools arrives. |
| **GitHub Copilot Chat** | Yes | Ferry builds a new VS Code chat document rather than replaying one it read, and writes the entry in the workspace's chat index that makes it appear in the list. |
| **OpenAI Codex** | Yes | Codex needs a session header, a row in its sessions table to offer the conversation, the records its screen draws as well as the ones it sends the model, and a time on every record. Ferry writes all of them. |
| **Google Antigravity** | Yes | A conversation is built as a database of its own, with its records encoded by Ferry, and then listed in the separate file Antigravity reads its list from. Tool calls become text, and no step claims Antigravity ran anything. |

**Importing a tool's own conversations back into it is not a conversion.** That
is an ordinary restore. It works for all four tools, and nothing on this page
applies to it.

### Every row once said no

Copilot was refused because Ferry could only write back a document it had
read. Codex, because its session header supposedly could not be made up.
Antigravity, because its conversations are encoded records inside a database.

Each reason sounded like a fact about the tool, and each turned out to be a gap
in what had been measured. In every case the real obstacle came **after** the
file was written: an entry in Copilot's chat index, a trusted folder in Claude
Code, a row in Codex's sessions table spelt exactly the way Codex spells a
path, and an entry in Antigravity's separate list. Each time, the conversation
was complete on disk and the tool's list was empty, which looks exactly like a
failed import.

## What is lost

Ferry counts these for **your** conversations and shows you the numbers before
it writes anything.

| What | What happens |
|---|---|
| **Reasoning signatures** | Dropped. The signature on the assistant's private reasoning is issued by the company whose model produced it, and nobody else can reissue it. Ferry will not fake one. |
| **Tool calls and results** | Kept as readable text. They are not calls the new assistant can run, and they are never presented as if they were. |
| **Who wrote it** | Unchanged. The conversation stays credited to the tool and model that produced it. Writing it into another tool does not make it that tool's conversation. |
| **Images** | Carried when their files are in the bundle, and dropped and counted when they are not. |

### Two ways to convert

| Mode | Keeps | Good for |
|---|---|---|
| **Archive**, the default | The most detail, including the assistant's reasoning where the target can hold it | Reading and searching your history |
| **Continue** | Everything said, without the private reasoning and the raw tool output | Carrying on working in the conversation |

Continue gives up more on purpose. An assistant that checks reasoning
signatures rejects a conversation carrying ones it did not issue, and a tool
call naming a tool it does not have describes something that could not have
happened there.

## Doing it

Conversion **never happens unless you ask for it.**

From the menu:

1. Run `ferry` and choose *Import a bundle into a tool*.
2. Choose the bundle and the assistant to import into.
3. If the bundle holds conversations from another assistant, Ferry says so,
   counts what converting them would cost, and asks how to bring them in:
   convert for reading, convert to carry on, or, when the bundle also holds the
   tool's own conversations, import only those. Nothing is written yet.
4. The next screen starts on *Preview it first*, which lists what would be
   written and what each conversation would lose, without writing anything.

From a command, `--allow-cross-tool` is the only way to say yes:

```bash
ferry import --bundle my-bundle --tool codex --allow-cross-tool --dry-run
```

Add `--mode continue` to convert for carrying on. Without `--allow-cross-tool`,
a bundle holding only another assistant's conversations is refused, naming both
tools, and nothing is written.

A conversation Ferry cannot convert is skipped with the reason. It is never
half written: an import that cannot produce a real conversation produces no
file at all.

## How a converted conversation is marked

Every conversion keeps a record of where the conversation came from, what it
was written into, which version of Ferry did it, and the full list of what was
lost. The notes kept are the same ones you were shown before you agreed.

This matters more than it sounds. A converted conversation that looked native
would, months later, be impossible to tell apart from one that really happened
in that tool, tool calls and all.

**The record is kept by Ferry, in `~/.ferry/provenance/`, not inside the other
tool's file.** That is a deliberate choice. The transcript belongs to the tool
you imported into, and the tool rewrites it whenever you carry on the
conversation, so a note of Ferry's inside it would probably disappear the first
time you used it. A record that vanishes on first use is worse than one that
never claimed to exist.

It follows that exporting through Ferry carries the origin along, because the
export reads the record back. Copying a transcript out of the tool by hand does
not: the file itself says nothing about where it came from.

## If you are restoring, not converting

Importing Claude Code conversations into Claude Code, Codex into Codex and so
on is a plain restore. Ferry never asks about conversion, and nothing above
applies.
