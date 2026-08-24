# Storage formats

Where each assistant keeps your conversations, and what shape they are in.

None of this is published by the tools' vendors. Every claim here was verified
against a real installation — the layout by reading it, the naming rules by
reading the shipped implementation and then reproducing them — and each section
records the version it was checked against. **Formats change without notice.**
If a section's version is far behind what you are running, treat it as a lead
rather than a fact.

| Assistant | Status | Verified against |
|---|---|---|
| Claude Code | Documented below | 2.1.229 – 2.1.237, Windows |
| OpenAI Codex | Documented below | 0.149.0-alpha.4.1, Windows |
| GitHub Copilot Chat | Not yet | — |
| Google Antigravity | Not yet | — |

---

## Claude Code

### Where it lives

```
%USERPROFILE%\.claude\                     $CLAUDE_CONFIG_DIR overrides this
    projects\
        <mangled-project-path>\
            <session-uuid>.jsonl           the conversation
            <session-uuid>\
                tool-results\*.txt         oversized tool output, spilled
%USERPROFILE%\.claude.json                 global config — a SIBLING of .claude\
```

On macOS and Linux the root is `~/.claude`. The path is NFC-normalised.

### The project directory name

Each project directory is named after the working directory the conversation
happened in — **not** a hash of it, despite appearances:

```
C:\Users\Dell\Desktop\Ferry   ->   C--Users-Dell-Desktop-Ferry
```

The rule is a single substitution: **every character that is not `a-z`, `A-Z` or
`0-9` becomes a hyphen.** Letter case is preserved. Runs of punctuation do not
collapse — one hyphen per character. Accented letters are not alphanumeric to
this rule and become hyphens too.

If the result exceeds **200 characters** it is truncated to 200 and a hash of
the *original* path is appended:

```
<first 200 chars>-<base36 of abs(int32 hash)>
```

The hash is the classic `h = h * 31 + code` over UTF-16 code units with signed
32-bit wraparound — the same value JavaScript's
`(h << 5) - h + charCodeAt(i) | 0` produces. It is taken over the full original
path, so two long paths that truncate to the same prefix still land in
different directories.

> **This substitution is lossy and cannot be reversed.** `a.b`, `a-b` and `a_b`
> all mangle to `a-b`. Ferry therefore derives the directory name from the
> *target* machine's working directory on import and never tries to decode the
> old one.

Two environment variables affect this. `CLAUDE_CONFIG_DIR` relocates the whole
`.claude` directory. `CLAUDE_CODE_PROJECT_DIR_NAME` pins the project directory
name outright — but only when `CLAUDE_CONFIG_DIR` is also set, and only if the
value matches `^[A-Za-z0-9_-]{1,64}$` and is not a reserved Windows device name
(`con`, `prn`, `aux`, `nul`, `com0`–`com9`, `lpt0`–`lpt9`).

### The transcript

JSONL: one JSON object per line, UTF-8, compact separators, non-ASCII written
raw rather than escaped.

**Not every line is a message.** Across 4,238 real records, ten record types
appeared:

| `type` | What it is |
|---|---|
| `user`, `assistant` | The conversation. Everything else is metadata. |
| `attachment` | Claude Code's own context injections — token reminders, skill listings, file edits. **Not user uploads.** |
| `ai-title`, `custom-title` | The generated and user-set titles. A custom title, when present, is the real one. |
| `last-prompt` | Pointer to the leaf of the current thread |
| `mode` | Permission-mode switches |
| `queue-operation` | Queued-command bookkeeping |
| `system` | Hook summaries, compact boundaries, local commands |
| `atis-latch` | Internal state |

A reader must tolerate types not on this list; the set has grown between
versions already.

Message records carry `cwd`, `gitBranch`, `version` (the Claude Code version
that wrote the line — a single session can span an upgrade), `uuid`,
`parentUuid` for threading, `sessionId`, and an ISO 8601 UTC `timestamp` with
milliseconds and a trailing `Z`.

### Content blocks

`message.content` is normally a list of blocks — **but 74 of 2,399 real
messages carried a bare string instead.** A reader that assumes a list drops
those messages and reports success.

| Block | Fields | Note |
|---|---|---|
| `text` | `text` | |
| `thinking` | `thinking`, `signature` | The text is under **`thinking`**, not `text` |
| `tool_use` | `id`, `name`, `input`, `caller` | |
| `tool_result` | `tool_use_id`, `content`, `is_error` | `content` is a string *or* a list |
| `image` | `source: {type, media_type, data}` | Base64, **inline in the transcript** — not a file on disk. Ferry extracts it into the bundle's `attachments/` and records its position with a UCS `image` block |

### Spilled tool output

When a tool result is too large to sit in the transcript, Claude Code writes it
to `projects\<mangled>\<session-uuid>\tool-results\<id>.txt` and leaves
`toolUseResult.persistedOutputPath` (an **absolute** path) plus
`persistedOutputSize` behind.

These files are part of the conversation. An export that leaves them behind
produces dangling references, and the absolute path is meaningless on another
machine — Ferry carries the files in the bundle and rewrites the pointer to
where they land.

### `~/.claude.json`

A sibling of `.claude\`, holding the OAuth account, machine and user ids,
cached feature flags, and a `projects` map **keyed by the raw absolute path**
(both separator spellings appear as separate keys). The values are per-project
*settings* — trust-dialog state, allowed tools, MCP config.

It is **not** a conversation index. Conversations appear because Claude Code
reads the `projects` directory, so Ferry does not write this file: the only
consequence of its absence is the first-run trust prompt, and the file also
holds credentials that nothing should be rewriting on a user's behalf.

---

## OpenAI Codex

Verified against Codex CLI **0.149.0-alpha.4.1** on Windows, over 25,884 real
records in 6 sessions.

### Where it lives

```
%USERPROFILE%\.codex\                    $CODEX_HOME overrides this
    sessions\YYYY\MM\DD\
        rollout-<ISO stamp>-<uuid>.jsonl   the conversation
    state_5.sqlite                         thread index (the _N moves)
    session_index.jsonl                    top-level thread list
    sqlite\codex-dev.db                    a third index: local_thread_catalog
    attachments\<uuid>\pasted-text.txt     pasted text
    generated_images\<call id>.png         images the model produced
```

The filename's timestamp uses dashes where ISO 8601 uses colons, because a
colon cannot appear in a Windows filename. It is not authoritative — the record
timestamps inside the file are.

**`~/.codex/history.jsonl` and `~/.codex/memories/` do not exist**, despite
being widely reported. Memories are in `memories_1.sqlite`.

### The transcript

JSONL, and unusually regular: **every record is exactly
`{type, timestamp, payload}`**, with no optional keys and no bare-string
variants. All variation lives in `payload`.

| `type` | What it is |
|---|---|
| `response_item` | **The canonical record.** Messages, tool calls, tool output. |
| `event_msg` | The interface's echo of the same turn. **Reading both double-counts the conversation.** |
| `session_meta` | The header. Line 1 is always one of these. |
| `turn_context` | Per-turn model, sandbox and approval settings |
| `world_state`, `compacted` | Workspace snapshot and compaction history |

Those last four carry structured config rather than a typed event, so
`payload.type` is **absent** — a reader must branch on `type` first and only
then on `payload.type`, or it loses all of them (944 of 25,884 records here).

### The two traps

**Reasoning is stored twice, and only one copy is readable.**

| | record | fields |
|---|---|---|
| `reasoning` | `response_item` | `encrypted_content`, `id`, `summary` — **no plaintext** |
| `agent_reasoning` | `event_msg` | `text` |

Mapping the canonical one produces reasoning full of ciphertext with a
perfectly plausible block count.

**MCP results are not always duplicates.** Of 461 `mcp_tool_call_end` records,
only 206 had a matching `call_id` in `response_item`. **255 existed nowhere
else**, so skipping `event_msg` wholesale loses real tool output.

### `session_meta`, which is validated strictly

`base_instructions` and `context_window` are **objects, not scalars**. Get
either shape wrong and Codex rejects the entire file — *"does not start with
session metadata"* — so the conversation disappears with no error and no
partial read. The session id must also be valid hex.

`session_id` is **not** a second copy of `id`: on a subagent thread it holds the
**parent** thread's id. Overwriting it reparents the subagent to itself.

### Images

`input_image` blocks carry `image_url` as a **`data:image/png;base64,…` URI** —
inline in the transcript. A sibling `local_images` array holds paths into
`%TEMP%`, and **all of them pointed at files that no longer existed**; the data
URI is the only surviving copy.

`user_message` also has `audio` and `local_audio` keys. Both were empty in all
440 records, but `dictation-history/` holds 177 files, so audio input exists on
this machine and simply did not reach a rollout. **Unverified, not absent.**

### Resuming

`codex exec resume <id>` resolves the transcript **from disk** — verified by
writing a rebuilt rollout into a scratch `CODEX_HOME` with an empty `threads`
table and watching Codex load it. `codex.exe` does contain
`SELECT rollout_path FROM threads WHERE id = ? AND archived = 0`, so the
database is presumably what the interactive picker and the Desktop list read;
that path is **untested**, because the picker cannot be driven headlessly.

### Size

One rollout was **53 MB**; six sessions totalled **121 MB**. Public reports
describe files an order of magnitude larger. Nothing may read one whole.

---

## GitHub Copilot Chat

Verified against VS Code **1.134.0** on Windows, over 135 delta records in 10
sessions.

**Copilot Chat is built into VS Code, not a marketplace extension.** There is
no extension version to pin — pin the VS Code version.

### Where it lives

```
%APPDATA%\Code\User\                        macOS: ~/Library/Application Support/Code/User
                                            Linux: ~/.config/Code/User
    workspaceStorage\<key>\
        chatSessions\<uuid>.jsonl             a conversation, folder open
        state.vscdb                           this workspace's chat list
        workspace.json                        which folder or .code-workspace
    workspaceStorage\vscode-chat-images\
        image-<epoch ms>.png                  shared across every workspace
    globalStorage\
        emptyWindowChatSessions\<uuid>.jsonl  a conversation, no folder open
        state.vscdb                           the no-folder chat list
        github.copilot-chat\session-store.db  Copilot CLI sessions — NOT read
```

`session-store.db` is a separate relational store belonging to Copilot CLI,
with its own WAL. Ferry v0.1 does not read it.

### The workspace key

The directory name under `workspaceStorage` is a digest, and **for a folder it
covers the folder's creation time as well as its path** — so it cannot be
derived from the path alone. Read from VS Code's `main.js` and verified against
four real directories.

| Window | Key |
|---|---|
| Folder open | `md5(fsPath + stamp)` — stamp is `st_ino` on Linux, birth time in ms on macOS and Windows |
| `.code-workspace` file | `md5(fsPath)`, lowercased except on Linux |
| No folder open | `Date.now() + random` — not derivable, and never needs to be |

`fsPath` uses backslashes and a **lowercase drive letter** on Windows.

On Windows, `st_birthtime` does not exist before Python 3.12; `st_ctime` is the
creation time there. On macOS `st_ctime` is the metadata-change time and must
not be substituted.

### The transcript is a log of edits, not a document

Every line is a change to the conversation built so far.

| `kind` | Meaning |
|---|---|
| 0 | Full snapshot — `v` replaces the document |
| 1 | Set `v` at path `k` |
| 2 | Append `v` to the list at path `k` |

**A reader must replay the whole file.** On the probe machine *every* session
had `requests: []` in its snapshot, with all real turns arriving as later
appends. Reading only the snapshot returns a well-formed conversation
containing no messages, and reports success.

**A `kind: 2` record may also carry `i`.** That is not where the new items go —
it is where the existing list is **cut** before they are added, because VS Code
revises a response while it streams and re-sends the tail. Ignoring it does not
lose data, it duplicates it: 44 blocks where the conversation held 34.

Only kinds 0, 1 and 2 have been observed. `i` appeared in none of the first 18
records examined and only showed up once a conversation used tools — assume the
set is incomplete.

### A turn

```
requestId, message {text, parts}, response[], modelId, timestamp,
responseTimestamp, variableData, hiddenFromTranscript, agent, result,
codeCitations, contentReferences, followups, modeInfo, modelState,
outputBuffer, promptTokens, completionTokens, timeSpentWaiting, ...
```

`hiddenFromTranscript` marks a turn Copilot hides from its own display.

### Response blocks — text has no discriminator

| `kind` | Maps to |
|---|---|
| *(absent)* | **Assistant text.** A `MarkdownString`: has `value`, has no `kind`. |
| `thinking` | Reasoning — but see below |
| `toolInvocationSerialized` | Tool call (`toolId`, `toolCallId`, `toolSpecificData`) and its `resultDetails` |
| `inlineReference` | A file or symbol named inside the prose |
| `mcpServersStarting`, `autoModeResolution` | Interface narration, not conversation |

**Three different key sets appear for the untagged text block** in real data,
differing only in which support flags are present. Match on *"has `value`, has
no `kind`"* — matching a key set drops the assistant's answer as soon as VS
Code adds a flag.

**Every `thinking` block observed was empty** — half `""`, half `[]`. Copilot
writes the marker without the reasoning.

### Images

Pasted images are stored **inline in the transcript**, base64 in
`variableData.variables[]` with `kind: "image"` and a `mimeType`. The copy in
`vscode-chat-images\` is not needed to recover them.

`kind: "file"` variables are *references* to files, holding a path and no
content.

### The chat list

`state.vscdb` is `ItemTable(key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)`.
The key `chat.ChatSessionStore.index` holds the list VS Code renders:

```json
{"version": 1, "entries": {"<uuid>": {
    "sessionId", "title", "lastMessageDate", "timing": {"created"},
    "initialLocation", "hasPendingEdits", "isEmpty", "isExternal",
    "lastResponseState", "permissionLevel"}}}
```

**There are two of these.** `globalStorage\state.vscdb` lists the no-folder
conversations; each workspace's own lists that workspace's. A transcript
written without its entry is a conversation VS Code will never show.

### Timestamps

Epoch **milliseconds**, unlike Claude Code's ISO strings.

### Size

Ten sessions totalled **1.65 MB**; the largest replayed to 403 KB. Small enough
that a bundle carries the whole replayed document.
