# Moving a conversation between tools

Ferry can write a conversation from one assistant into another's history. This
page is what it costs and what works, because both answers are narrower than
"yes".

## What you get

**A readable transcript in the target tool. Not a session its assistant can
pick up and continue.**

That is the honest ceiling and it is worth reading twice. Converting a Claude
Code conversation into another tool gives you something you can open, scroll and
search there. It does not give that tool's assistant a session it can resume:
the tool calls in it were made by a different assistant against a different set
of tools, and no amount of format translation changes that.

If what you want is your history preserved and searchable, conversion does that.
If what you want is to hand a half-finished task to a different assistant,
[Compact](../README.md) is the better tool — it produces a handoff document you
can paste into any of them.

## What works

Measured against real conversations, not assumed. Each pair was imported into a
scratch store and then **exported back out again**, because "the import
reported success" and "the conversation is really there" are different claims.

| Into | Works | Why |
|---|---|---|
| **Claude Code** | **Yes** | Its transcript is plain JSONL and every envelope field can be rebuilt. All three other tools, every message, 100% of the words. |
| OpenAI Codex | No | Codex rebuilds a conversation from the `session_meta` header in its own rollout file. A bundle from another tool does not carry one, and the header cannot be invented — Codex rejects the whole file if it is wrong. |
| GitHub Copilot Chat | Not yet | Copilot Chat stores a conversation as a VS Code transcript document, and Ferry can only write back one it read. It cannot yet build one for a conversation that came from another tool. The chat index is not the obstacle: Ferry already writes it, and every field in it has an obvious default. |
| Antigravity | No | Antigravity conversations are restored from the original SQLite database they were exported with. Ferry can carry one across; it cannot build one for a conversation that never had it. |

**Importing a tool's own conversations back into it is not a conversion** and is
unaffected by any of this. That is an ordinary restore and it works for all
four tools.

### Why some say "no" and one says "not yet"

Codex and Antigravity are refused because of how those tools store their data,
not because of a missing feature in Ferry: Codex rejects a rollout whose header
it did not write, and an Antigravity conversation is restored from a database
that has to already exist.

**Copilot is different, and the table says so.** Nothing there is unsolved —
Ferry already derives the workspace key and writes the chat index for Copilot's
own conversations. What is missing is the step that builds an index entry for a
conversation that came from somewhere else. That is work, not a wall.

The distinction is kept because a person deciding what to do with their history
is served by a straight answer, and "cannot" and "not yet" are different
answers.

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
