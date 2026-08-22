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
| OpenAI Codex | Not yet | — |
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
| `image` | `source: {type, media_type, data}` | Base64, **inline in the transcript** — not a file on disk |

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
