# Moving a conversation between tools

Ferry can write a conversation from one assistant into another's history. This
page is what it costs and what works, because both answers are narrower than
"yes".

## What you get

**A conversation you can open, search, and keep working in.**

That ceiling used to be lower. This page said a converted conversation was a
readable transcript and nothing more, because that is what had been measured at
the time. It has since been tested in all three targets — someone typed into a
converted conversation in Claude Code, in Copilot Chat and in Codex, and the
assistant answered from the history it had been given.

**What does not come with it is the session.** The tool calls in a converted
conversation were made by a different assistant against a different set of
tools; Ferry carries them as readable text and does not present them as calls
the new tool can re-run. Nothing reconnects to a shell, a file watcher or an
MCP server that was live somewhere else.

So: if you want your history preserved, searchable and live in a new tool,
conversion does that. If you want a short handoff to paste as the first message
of a fresh session rather than the whole history,
[Compact](../README.md) still does that better.

## What works

Measured against real conversations, not assumed. Each pair was imported into a
scratch store and then **exported back out again**, because "the import
reported success" and "the conversation is really there" are different claims.

| Into | Works | Why |
|---|---|---|
| **Claude Code** | **Yes** | Its transcript is plain JSONL and every envelope field can be rebuilt. All three other tools, every message, 100% of the words. |
| **GitHub Copilot Chat** | **Yes** | Ferry builds a VS Code chat document rather than replaying one it read, and writes the workspace chat index entry that makes it appear in the list. A question is carried in the document's `parts`, not only its `text`, because that is what VS Code draws. |
| **OpenAI Codex** | **Yes** | The `session_meta` header *can* be synthesised — measured, not assumed. Codex also needs a row in its `threads` table to offer the conversation in the picker, the interface events it draws each turn as well as the records it sends the model, and a timestamp on every record. All four are written. |
| Antigravity | No | Antigravity conversations are restored from the original SQLite database they were exported with. Ferry can carry one across; it cannot build one for a conversation that never had it. |

**Importing a tool's own conversations back into it is not a conversion** and is
unaffected by any of this. That is an ordinary restore and it works for all
four tools.

### Why the one "no" is a no

Antigravity is refused because of how that tool stores its data, not because of
a missing feature in Ferry: a conversation there is restored from a database
that has to already exist, and Ferry will not invent one.

**Two of these rows used to say no.** Codex was refused on the grounds that its
header could not be invented, and Copilot on the grounds that Ferry could only
write back a document it had read. Both turned out to be work rather than
walls, and both took a measurement to find out — which is the reason this table
distinguishes a tool's own storage from a gap in Ferry, and the reason the
Antigravity row is worth re-testing rather than treating as permanent.

Every one of the three that works needed something **after** the file was
written before the tool would admit it existed: a Copilot chat index entry, a
trusted folder in Claude Code, a `threads` row in Codex. In each case the
conversation was complete on disk and the tool's list was empty, which looks
exactly like the import having failed.

## What is lost

Ferry counts these for **your** conversation and shows the numbers before it
writes anything. Nothing here is guessed at afterwards.

| What | What happens |
|---|---|
| **Thinking signatures** | Dropped. The signature on a thinking block is issued by the vendor whose model produced it and cannot be reissued elsewhere. Ferry will not fabricate one. |
| **Tool calls and results** | Kept as readable text. They are not tool calls the target can run, and they are not presented as though they were. |
| **Attribution** | Unchanged. The conversation stays attributed to the tool and the model that produced it. Writing it into another tool does not make it that tool's conversation. |
| **Images** | Carried when their bytes are in the bundle, dropped and counted when they are not. |

## Doing it

Conversion is **refused by default**. You have to ask.

1. `ferry`, then *Import conversations from a bundle*.
2. Choose the bundle and the assistant to import into.
3. If the bundle holds conversations from another tool, Ferry says so, counts
   what converting them would cost, and asks. The option under the cursor is
   *import only the ones that came from this assistant* — the one that converts
   nothing.
4. Choose *Preview it first* on the next screen to see the full list of what
   would be written and what each conversation would lose, without writing
   anything.

A conversation Ferry cannot convert is skipped with the reason from the table
above. It is never half-written: an import that cannot produce a real
conversation produces no file at all.

## How a converted conversation is marked

Every cross-tool write records a `provenance` block: where the conversation
came from, what it was written into, which version of Ferry did it, and the
full list of what was lost. The notes recorded are the same notes you were
shown before agreeing.

This matters more than it sounds. A converted conversation that looked native
would, months later, be indistinguishable from one that really happened in that
tool — including its tool calls, which never ran there.

**The record is kept by Ferry, in `~/.ferry/provenance/`, not inside the other
tool's file.** That is a deliberate limit rather than a shortcut. The transcript
belongs to the tool you imported into, and that tool rewrites it whenever you
resume the conversation; a field of Ferry's own would very likely be dropped by
the first such rewrite, leaving the conversation unmarked with nothing to say
it had ever been marked. A record that vanishes the first time you use the
conversation is worse than one that never claimed to exist.

What follows from that: exporting through Ferry carries the origin with it,
because the export reads the record back. Copying a transcript out of the tool
by hand does not — the file itself says nothing, and Ferry cannot make it say
anything it will keep.

## If you are restoring, not converting

You are almost certainly not on this page for a reason. Importing Claude Code
conversations into Claude Code, Codex into Codex, and so on is a plain restore,
Ferry never asks about conversion, and nothing above applies.
