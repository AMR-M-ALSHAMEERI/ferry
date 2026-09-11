# Storage formats

Where each assistant keeps your conversations, and what shape they are in.

None of this is published by the tools' makers. Every claim here was checked
against a real installation: the layout by reading it, and the naming rules by
reading the shipped code and then reproducing them. Each section records the
version it was checked against. **Formats change without notice.** If a
section's version is far behind the one you are running, treat it as a lead
rather than a fact.

| Assistant | Checked against |
|---|---|
| Claude Code | 2.1.229 to 2.1.237, Windows |
| OpenAI Codex | 0.149.0-alpha.4.1, Windows |
| GitHub Copilot Chat | VS Code 1.134.0, Windows |
| Google Antigravity | 2.8.1, Windows |

## Claude Code

### Where it lives

```text
%USERPROFILE%\.claude\                     $CLAUDE_CONFIG_DIR moves this
    projects\
        <mangled-project-path>\
            <session-uuid>.jsonl           the conversation
            <session-uuid>\
                tool-results\*.txt         oversized tool output, stored apart
%USERPROFILE%\.claude.json                 global settings, BESIDE .claude\
```

On macOS and Linux the root is `~/.claude`. The path is NFC normalised.

### The project folder's name

Each project folder is named after the working directory the conversation
happened in. It looks like a hash, and it is **not** one:

```text
C:\Users\Dell\Desktop\Ferry   ->   C--Users-Dell-Desktop-Ferry
```

The rule is one substitution: **every character that is not `a-z`, `A-Z` or
`0-9` becomes a hyphen.** Letter case is kept. A run of punctuation does not
collapse: each character becomes its own hyphen. Accented letters count as
punctuation under this rule and become hyphens too.

If the result is longer than **200 characters**, it is cut to 200 and a hash of
the *original* path is added:

```text
<first 200 chars>-<base36 of abs(int32 hash)>
```

The hash is the classic `h = h * 31 + code` over UTF-16 code units, with signed
32 bit wraparound, the same value JavaScript's
`(h << 5) - h + charCodeAt(i) | 0` produces. It is taken over the whole original
path, so two long paths that cut to the same 200 characters still land in
different folders.

> **This substitution loses information and cannot be reversed.** `a.b`, `a-b`
> and `a_b` all become `a-b`. So Ferry works out the folder name from the
> *target* machine's working directory when importing, and never tries to
> decode the old one.

Two environment variables affect this. `CLAUDE_CONFIG_DIR` moves the whole
`.claude` folder. `CLAUDE_CODE_PROJECT_DIR_NAME` sets the project folder's name
outright, but only when `CLAUDE_CONFIG_DIR` is also set, only if the value
matches `^[A-Za-z0-9_-]{1,64}$`, and only if it is not a reserved Windows device
name (`con`, `prn`, `aux`, `nul`, `com0` to `com9`, `lpt0` to `lpt9`).

### The transcript

JSONL: one JSON object per line, UTF-8, compact separators, and non-ASCII
characters written as they are rather than escaped.

**Not every line is a message.** Across 4,238 real records, ten record types
appeared:

| `type` | What it is |
|---|---|
| `user`, `assistant` | The conversation. Everything else is metadata. |
| `attachment` | Context Claude Code adds itself, such as reminders, skill listings and file edits. **Not files the person uploaded.** |
| `ai-title`, `custom-title` | The generated title and the one the person set. A custom title, when there is one, is the real one. |
| `last-prompt` | Points at the latest message of the current thread |
| `mode` | Changes of permission mode |
| `queue-operation` | Bookkeeping for queued commands |
| `system` | Hook summaries, compaction markers, local commands |
| `atis-latch` | Internal state |

A reader must cope with types not on this list. The set has already grown
between versions.

Message records carry `cwd`, `gitBranch`, `version` (the Claude Code version
that wrote the line, since one session can span an upgrade), `uuid`,
`parentUuid` for threading, `sessionId`, and a `timestamp` in ISO 8601 UTC with
milliseconds and a trailing `Z`.

### Content blocks

`message.content` is normally a list of blocks, **but 74 of 2,399 real messages
held a plain string instead.** A reader that expects a list drops those
messages and still reports success.

| Block | Fields | Note |
|---|---|---|
| `text` | `text` | |
| `thinking` | `thinking`, `signature` | The text is under **`thinking`**, not `text` |
| `tool_use` | `id`, `name`, `input`, `caller` | |
| `tool_result` | `tool_use_id`, `content`, `is_error` | `content` is a string *or* a list |
| `image` | `source: {type, media_type, data}` | Base64, **inside the transcript**, not a file on disk. Ferry extracts it into the bundle's `attachments/` and records its position with a UCS `image` block |

### Tool output stored apart

When a tool result is too large to keep in the transcript, Claude Code writes it
to `projects\<mangled>\<session-uuid>\tool-results\<id>.txt` and leaves
`toolUseResult.persistedOutputPath` (an **absolute** path) and
`persistedOutputSize` in its place.

These files are part of the conversation. An export that leaves them behind
produces broken references, and an absolute path means nothing on another
machine. Ferry carries the files in the bundle and points the reference at
where they land.

### `~/.claude.json`

It sits beside `.claude\`, and holds the OAuth account, machine and user ids,
cached feature flags, and a `projects` map **keyed by the raw absolute path**
(both separator spellings appear as separate keys). The values are settings per
project: the trust question's answer, allowed tools, MCP configuration.

It is **not** a list of conversations. Conversations appear because Claude Code
reads the `projects` folder. Ferry only writes to this file when a person
agrees, from the menu, to let Claude Code open the folders an import wrote
into, and it saves the previous file to the backups first. A command import
never touches it. The file also holds credentials, which nothing should be
rewriting on someone's behalf without asking.

## OpenAI Codex

Checked against Codex CLI **0.149.0-alpha.4.1** on Windows, over 25,884 real
records in 6 sessions.

### Where it lives

```text
%USERPROFILE%\.codex\                    $CODEX_HOME moves this
    sessions\YYYY\MM\DD\
        rollout-<ISO stamp>-<uuid>.jsonl   the conversation
    state_5.sqlite                         session index (the number changes)
    session_index.jsonl                    list of top-level sessions
    sqlite\codex-dev.db                    a third index: local_thread_catalog
    attachments\<uuid>\pasted-text.txt     pasted text
    generated_images\<call id>.png         images the model produced
```

The file name's timestamp uses hyphens where ISO 8601 uses colons, because a
Windows file name cannot contain a colon. The name is not authoritative: the
timestamps on the records inside are.

**`~/.codex/history.jsonl` and `~/.codex/memories/` do not exist**, although
they are widely reported. Memories are kept in `memories_1.sqlite`.

### The transcript

JSONL, and unusually regular: **every record is exactly
`{type, timestamp, payload}`**, with no optional keys and no plain string
variants. All the variation is inside `payload`.

| `type` | What it is |
|---|---|
| `response_item` | **The main record.** Messages, tool calls, tool output. |
| `event_msg` | The interface's copy of the same turn. **Reading both counts the conversation twice.** |
| `session_meta` | The header. Line 1 is always one of these. |
| `turn_context` | Model, sandbox and approval settings for each turn |
| `world_state`, `compacted` | A snapshot of the workspace, and compaction history |

The last four hold structured settings rather than a typed event, so
`payload.type` is **missing** from them. A reader must look at `type` first and
only then at `payload.type`, or it loses all four (944 of 25,884 records
here).

### The two traps

**Reasoning is stored twice, and only one copy can be read.**

| | Record | Fields |
|---|---|---|
| `reasoning` | `response_item` | `encrypted_content`, `id`, `summary`, and **no readable text** |
| `agent_reasoning` | `event_msg` | `text` |

Reading the main record gives reasoning full of ciphertext, with a perfectly
believable block count.

**MCP results are not always copies.** Of 461 `mcp_tool_call_end` records, only
206 had a matching `call_id` among the main records. **255 existed nowhere
else**, so skipping every `event_msg` loses real tool output.

### `session_meta`, which is checked strictly

`base_instructions` and `context_window` are **objects, not single values**.
Get either shape wrong and Codex rejects the whole file, saying it *"does not
start with session metadata"*, so the conversation disappears with no error and
nothing partly read. The session id must also be valid hexadecimal.

`session_id` is **not** a second copy of `id`. In a subagent's session it holds
the **parent** session's id, and overwriting it makes the subagent its own
parent.

### Not every session file is a conversation

**A subagent gets its own session file**, in the same date folders, with the
same name pattern and the same records as a conversation somebody had. Codex
never offers it to resume. On the reference machine that was **1 file of 6**,
so counting files gave six sessions where Codex lists five.

Three fields in `session_meta` agree when a session is a subagent's:

| Field | In a conversation | In a subagent |
|---|---|---|
| `thread_source` | `user` | `subagent` |
| `source` | the string `vscode` | an object, such as `{"subagent": {"other": "guardian"}}` |
| `parent_thread_id` | missing | the uuid of the session that started it |

`parent_thread_id` is **not always there**. It was missing from the older of
the two subagent headers examined, where the parent's id was in `session_id`
instead. So the *decision* is made on `thread_source` or `source`, and the
parent's id is read from `parent_thread_id` first and `session_id` second.

`$CODEX_HOME/session_index.jsonl` is Codex's own list, with `id`,
`thread_name` and `updated_at`, and it held exactly the five sessions that were
not subagents. It is used **only to confirm**: it is a cache Codex keeps, while
the header is the file describing itself.

### Images

`input_image` blocks carry `image_url` as a **`data:image/png;base64,…` URI**,
inside the transcript. A neighbouring `local_images` list holds paths into
`%TEMP%`, and **every one of them pointed at a file that no longer existed**.
The data URI is the only copy left.

`user_message` also has `audio` and `local_audio` keys. Both were empty in all
440 records, but `dictation-history/` holds 177 files, so voice input exists on
this machine and simply never reached a session file. **Not checked, rather than
absent.**

### Resuming, and being offered to resume

**These are two different things, and the difference cost a whole phase of
work.** Measured on Codex 0.147. The description this replaces was measured on
0.98 and had gone out of date.

`codex exec resume <id>` finds the transcript **on disk**, with the sessions
table empty. That was true then and is still true, and it is why a check done
by id passed while the feature was broken.

**The picker reads the database.** With ten session files on disk and six rows
in the table, the six were exactly what Codex offered. A session with no row can
be opened only by someone who knows its uuid, which nobody moving their history
does. The row is what makes a conversation *findable*, and writing one is part
of writing a conversation.

What a rebuilt session needs before Codex will show it:

| What | Why |
|---|---|
| A row in `threads` | The picker and the desktop list read the database, not the date folders. |
| `event_msg` records | A session keeps two accounts of each turn: `response_item` is what is sent to the model, and `event_msg` is what the interface **draws**. Write only the first and the conversation resumes with half the screen empty. |
| A turn around each exchange | `task_started` and `task_complete` sharing a `turn_id`, which the assistant's message repeats in `internal_chat_message_metadata_passthrough`. An assistant message outside any turn belongs to nothing the app can draw. Its `phase` field decides whether it is shown. |
| A `cwd` spelt the way Codex spells it | The command line picker offers the sessions for the folder you are in, and it compares the stored text. On Windows, Codex writes the extended form, `\\?\C:\...`, in 7 of 7 rows measured. A plain `C:\...` is the same folder and a different string, so the picker matches nothing and shows an empty list. |
| A `timestamp` on **every** record | Not only the ones whose message carried a time. Copilot records no time for each message, so a conversation converted from it produced records without the field, and Codex read none of them. |

**The row can be perfect and still be invisible,** which is the sharpest form of
this lesson the project has met. The `cwd` spelling was only found because
someone imported through the menu into a scratch home and opened the picker. A
test that writes a row and reads it back agrees with itself about the spelling,
and every automated check did.

`session_meta` **can be built from nothing** rather than borrowed from a real
session. The strictness described above is about the *shape* of
`base_instructions` and `context_window`, not about where the header came
from. Codex does not check that it wrote the header itself.

### Size

One session file was **53 MB**, and six sessions came to **121 MB**. Public
reports describe files ten times larger. Nothing may read one whole.

## GitHub Copilot Chat

Checked against VS Code **1.134.0** on Windows, over 135 change records in 10
sessions.

**Copilot Chat is built into VS Code, not installed as an extension.** There is
no extension version to note, so note the VS Code version.

### Where it lives

```text
%APPDATA%\Code\User\                        macOS: ~/Library/Application Support/Code/User
                                            Linux: ~/.config/Code/User
    workspaceStorage\<key>\
        chatSessions\<uuid>.jsonl             a conversation, with a folder open
        state.vscdb                           this workspace's chat list
        workspace.json                        which folder or .code-workspace
    workspaceStorage\vscode-chat-images\
        image-<epoch ms>.png                  shared by every workspace
    globalStorage\
        emptyWindowChatSessions\<uuid>.jsonl  a conversation, with no folder open
        state.vscdb                           the chat list for no folder
        github.copilot-chat\session-store.db  Copilot CLI sessions, NOT read
```

`session-store.db` is a separate database belonging to Copilot CLI, with its own
journal. Ferry 0.1 does not read it.

### The workspace key

The folder name under `workspaceStorage` is a digest, and **for a folder it
includes the folder's creation time as well as its path**, so it cannot be
worked out from the path alone. This was read from VS Code's `main.js` and
checked against four real folders.

| Window | Key |
|---|---|
| Folder open | `md5(fsPath + stamp)`, where the stamp is `st_ino` on Linux and the creation time in milliseconds on macOS and Windows |
| `.code-workspace` file | `md5(fsPath)`, in lower case except on Linux |
| No folder open | `Date.now() + random`, which cannot be worked out and never needs to be |

On Windows, `fsPath` uses backslashes and a **lower case drive letter**.

On Windows, `st_birthtime` does not exist before Python 3.12, and `st_ctime` is
the creation time there. On macOS, `st_ctime` is the time the file's details
last changed, and must not be used instead.

### The transcript is a list of changes, not a document

Every line is a change to the conversation built so far.

| `kind` | Meaning |
|---|---|
| 0 | A full snapshot: `v` replaces the document |
| 1 | Set `v` at path `k` |
| 2 | Append `v` to the list at path `k` |

**A reader must replay the whole file.** On the machine studied, *every*
session had `requests: []` in its snapshot, and all the real turns arrived as
later appends. Reading only the snapshot returns a well formed conversation with
no messages in it, and reports success.

**A `kind: 2` record may also carry `i`.** That is not where the new items go.
It is where the existing list is **cut** before they are added, because VS Code
revises a response while it streams and sends the end again. Ignoring it does
not lose data, it duplicates it: 44 blocks where the conversation held 34.

Only kinds 0, 1 and 2 have been seen. `i` appeared in none of the first 18
records examined and only showed up once a conversation used tools, so assume
the list is incomplete.

### A turn

```text
requestId, message {text, parts}, response[], modelId, timestamp,
responseTimestamp, variableData, hiddenFromTranscript, agent, result,
codeCitations, contentReferences, followups, modeInfo, modelState,
outputBuffer, promptTokens, completionTokens, timeSpentWaiting, ...
```

`hiddenFromTranscript` marks a turn Copilot hides from its own display.

### Response blocks, where text has no label

| `kind` | Becomes |
|---|---|
| *(missing)* | **The assistant's text.** A `MarkdownString`: it has `value` and no `kind`. |
| `thinking` | Reasoning, but see below |
| `toolInvocationSerialized` | A tool call (`toolId`, `toolCallId`, `toolSpecificData`) and its `resultDetails` |
| `inlineReference` | A file or symbol named inside the text |
| `mcpServersStarting`, `autoModeResolution` | Status messages from the interface, not conversation |

**Three different sets of keys appear for the unlabelled text block** in real
data, differing only in which support flags are present. Match on *"has
`value`, has no `kind`"*. Matching one set of keys loses the assistant's answer
the moment VS Code adds a flag.

**Every `thinking` block seen was empty,** half of them `""` and half `[]`.
Copilot writes the marker without the reasoning.

### Images

Pasted images are stored **inside the transcript**, as base64 in
`variableData.variables[]` with `kind: "image"` and a `mimeType`.

**The transcript is the copy to trust, not `vscode-chat-images\`.** That folder
is temporary. It held two images during this work, and a few hours later VS
Code had removed it entirely, while both images could still be recovered from
the transcripts. An adapter that read the folder would have started returning
nothing, with no error.

`kind: "file"` variables are *references* to files: they hold a path and no
content.

### The chat list

`state.vscdb` is `ItemTable(key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)`.
The key `chat.ChatSessionStore.index` holds the list VS Code shows:

```json
{"version": 1, "entries": {"<uuid>": {
    "sessionId", "title", "lastMessageDate", "timing": {"created"},
    "initialLocation", "hasPendingEdits", "isEmpty", "isExternal",
    "lastResponseState", "permissionLevel"}}}
```

**There are two of these.** `globalStorage\state.vscdb` lists the conversations
with no folder open, and each workspace's own file lists that workspace's. A
transcript written without its entry is a conversation VS Code will never show.

### Timestamps

Milliseconds since the epoch, unlike Claude Code's ISO strings.

### Size

Ten sessions came to **1.65 MB**, and the largest replayed to 403 KB. That is
small enough for a bundle to carry the whole replayed document.

## Google Antigravity

Checked against **2.8.1 on Windows**.

The hardest of the four, and hard in its own way: nothing is hidden or
encrypted, but nothing is plain text either.

### Layout

```text
~/.gemini/antigravity/conversations/<uuid>.db          one SQLite database per conversation
~/.gemini/antigravity/brain/<uuid>/                    that conversation's working folder
    .user_uploaded/media*.png                          images the person attached
    .system_generated/logs/transcript.jsonl            a plain JSON log of the same conversation
~/.gemini/config/projects/<project-uuid>.json          the folder a conversation belongs to
```

The path is the same on every operating system, unlike the other three tools.

There is **one database per conversation**, not one file holding many. Each
`.db` comes with `-wal` and `-shm` files that are part of the same database. The
newest messages of an open conversation live in the `-wal` file, so anything
that copies only the `.db` gets a conversation missing its latest turns, and
looks entirely successful doing it.

Each `brain/<uuid>/` folder is also a git repository. It tracks the files the
agent edited, not the conversation, so Ferry neither reads nor recreates it.

### Not every database is a conversation

**This is the first thing to get right, because getting it wrong triples the
count.**

When the agent starts a subagent, that subagent gets its **own database** in
`conversations/`, with the same tables, the same step types and its own uuid.
Antigravity never lists it, because it is part of the conversation that started
it. On the reference machine there were **6 databases for the 2 conversations
the app shows**: three subagents under one, and one under the other.

Nothing in a database's own metadata tells them apart. `trajectory_type`,
`source` and `has_subtrajectory` are the same in all six, and
`parent_references` is empty in every subagent.

The link is in `trajectory_metadata_blob`, where two fields point at it from
opposite ends:

| Field | In a top level conversation | In a subagent |
|---|---|---|
| 5 | missing | the parent's uuid |
| 6 | the conversation's own uuid | the parent's uuid |

Both agree in all six, so Ferry requires both, and treats any disagreement as a
top level conversation. Showing a subagent does far less harm than hiding
something the person wrote.

Counting `INVOKE_SUBAGENT` (127) steps in the parent does **not** work: one
parent has 2 such steps and 3 children.

The subagent databases still have to be carried. The parent conversation refers
to them, so restoring it without them leaves a conversation with pieces
missing.

### Protobuf inside SQLite

The tables are ordinary. The columns are not. `steps`, `gen_metadata`,
`executor_metadata` and `trajectory_metadata_blob` hold **protobuf blobs**, and
that is where the conversation is. On the reference machine, 7,716 of 7,716
blobs decode. **Nothing is compressed**: 18 byte sequences looked like gzip or
zlib headers, and none of them decompressed.

Every payload starts `08 XX`: protobuf field 1, a varint, holding the step type
itself. That makes each blob identify itself.

`steps.step_payload` behaves as a oneof without being declared as one. Field 5
holds metadata common to every step, and each step type puts its content at
**its own field number**, so the paths share no common start:

| Step type | Text at |
|---|---|
| USER_INPUT (14) | `19.2`, what the person typed |
| PLANNER_RESPONSE (15) | `20.1` and `20.8`, the same text twice |
| RUN_COMMAND (21) | `56.21.1.1` |
| SYSTEM_MESSAGE (101) | `114.4.3` |

`steps.metadata` field `1.1` is the step's time in **seconds** since the epoch.
It is present in every step of every conversation.

### The step types, and the one nobody can name

`steps.step_type` is an undocumented number. Seventeen of its values are known:
5 CODE_ACTION, 7 GREP_SEARCH, 8 VIEW_FILE, 9 LIST_DIRECTORY, 14 USER_INPUT, 15
PLANNER_RESPONSE, 17 ERROR_MESSAGE, 21 RUN_COMMAND, 23 CHECKPOINT, 31
READ_URL_CONTENT, 33 SEARCH_WEB, 91 GENERATE_IMAGE, 98 CONVERSATION_HISTORY, 101
SYSTEM_MESSAGE, 127 INVOKE_SUBAGENT, 132 GENERIC, 138 ASK_QUESTION.

**Type 28 is not among them.** It appears in the databases and never once in
the transcript, so there is no evidence of what it is, and it is left unnamed
rather than given a name that merely sounds right.

### The transcript is for checking, not for reading from

`brain/<uuid>/.system_generated/logs/transcript.jsonl` is plain JSON with
`{type, source, status, step_index, created_at, content, tool_calls?,
thinking?, truncated_fields?}`. Because it names its step types in words, and
`step_index` refers to `steps.idx`, it is how every field above was worked out
rather than guessed.

Two things stop it replacing the database:

- **It loses information.** 9.8% of its entries carry `truncated_fields`.
- **Its `content` is a rendering, not the stored text,** and it is often
  *longer* than the field it came from. Only 3 of 118 of the person's messages
  were 95% covered by the stored text, and the middle value was 57.7%.

`step_index` is also **many to one**: one step can produce several transcript
lines. Anything that assumes one line per step will report conflicts that are
not conflicts.

So the database holds what was stored, the transcript holds how it was shown,
and neither alone rebuilds what the person saw.

### Absolute paths, spelt four ways

Paths are stored *inside* the blobs, at **at least fourteen different field
paths**, three of them with more than 900 occurrences. The same path is also
written four different ways:

```text
C:\Users\Dell\Project                    plain, backslashes
C:/Users/Dell/Project                    plain, forward slashes
file:///c:/Users/Dell/Project            URI, lower case drive
file:///c%3A%5CUsers%5CDell%5CProject    URI, percent encoded, backslashes
```

The drive letter's case varies between them, even within one database.

**A path cannot be changed with a simple search and replace on the bytes.**
Every field of variable length is preceded by its length, and every enclosing
message by its own, so changing a path's length leaves a chain of lengths
describing a message that no longer exists. The blob still opens, and decodes
into nonsense. Changing a path means decoding, editing and encoding again.

### Projects

`trajectory_metadata_blob` field 18 names a project, and
`~/.gemini/config/projects/<id>.json` describes it. The folder is a
`folderUri` in the percent encoded spelling, either directly or inside
`gitFolder` when the folder is a git repository. A conversation whose project
does not exist has nowhere to be filed.

### Two stores, and the list is in the other one

**The conversations folder is not what Antigravity lists.** This was learned the
hard way: an exact copy of a conversation the app shows, with only its ids
changed, does not appear. Four rounds of building a conversation from nothing
had been spent guessing at the format before that copy was tried.

The language server names both stores in its own log:

```text
Creating trajectory store manager with proto store and SQLite store
```

The SQLite store is `conversations/<uuid>.db`. The proto store is
`~/.gemini/antigravity/agyhub_summaries_proto.pb`, a repeated field with one
entry per conversation, subagents included. **A conversation with no entry here
is invisible, however correct its database is.**

An entry is `{1: <conversation id>, 2: <summary>}`. Every part of the summary
was worked out by measuring the six real ones:

| Field | What it holds |
|---|---|
| 1 | The title |
| 2 | **The number of steps**: 2161, 9, 456, 14, 67 and 18, matching the databases exactly |
| 3, 7, 10 | Updated, created, and created again |
| 4 | A cascade id, different from the conversation id |
| 5, 22 | `1` and `4` in all six |
| 9 | The model identifier, in the same wrapper the trajectory blob uses |
| 15, 16 | A flag empty on half of them, and a count that is `0` below a hundred steps |
| 17 | The trajectory metadata, without the two fields it does not carry |

Field 23 appears in four of the six and is `0`. Ferry does not write it: the
entry copied to prove a conversation could be listed did not have it.

**Antigravity keeps this file in memory and writes it back when it exits,** so
an entry added while it is running is thrown away.

### What a conversation needs, built from nothing

Seven tables, with a schema simple enough to create directly (no migration
records, `user_version = 1`), and two blobs that matter.

`trajectory_metadata_blob` for a **top level** conversation carries exactly the
fields `[1, 2, 3, 6, 7, 10, 15, 18]`, the same in both measured. One built by
Ferry is **identical to a real one except for field 15**: 352 to 380 bytes that
do not decode as a message and differ in every conversation. Ferry leaves it
out rather than inventing it, and a conversation without it opens and reads.

**Field 5 must be missing.** It names a parent, which makes the conversation a
subagent, and subagents are never listed. That trap cost two rounds, because the
smallest database on a machine is usually a subagent, and so the worst possible
one to copy from.

Field 10 is `8a01067a042a020a00` in both top level conversations, byte for byte,
and is written as the constant it is. Field 7 is a model or resource identifier
of 39 to 44 characters, in eight parts separated by `/`, with no digits. It is
copied from an existing conversation rather than made up, because naming a
model for work done in another tool would be a claim Ferry cannot support.

**A step from the person keeps its text twice,** at `19.2` and at `19.3.1`,
byte for byte the same: 8,872 bytes each in the first one measured. Writing only
`19.2` gives a conversation whose title bar shows the question and whose message
bubble is empty. One copy is what is sent to the model, and the other is what
the interface draws.

### Attachments

`brain/<uuid>/.user_uploaded/`, as real files rather than base64 inside the
transcript. Two file name spellings appear in roughly equal numbers,
`media_<epoch_ms>.png` **and** `media__<epoch_ms>.png` with two underscores.
Nothing records which message an upload belonged to.

### Size

Six conversations came to **31.9 MB** across 2,725 steps. Most of it is
CHECKPOINT steps, which hold snapshots of files rather than conversation, and
reach 758 KB in a single blob.
