# Codex fixtures

Anonymised session files. **Every field name, type and level of nesting is
copied from a count of 25,884 real records** written by Codex CLI 0.145 to
0.149 on Windows. **Every piece of content is invented.** No line here came from
a real conversation.

| File | What it is for |
|---|---|
| `basic.jsonl` | An ordinary session, including the traps: encrypted `reasoning` beside readable `agent_reasoning`, and `event_msg` copies of turns that `response_item` already holds |
| `edge.jsonl` | An MCP result that repeats a `response_item` output and one that exists nowhere else; an inline image; a subagent header whose `session_id` is the *parent* session; unknown record, payload and content block types; a malformed line; a bare array; and the four record types with no `payload.type` |
| `empty.jsonl` | Metadata only, with no messages. A session that must be skipped rather than exported empty |

Three details these fixtures exist to hold in place, each of which silently
destroys data if it is assumed rather than checked:

- `reasoning` carries `encrypted_content` and **no readable text**. The
  readable text is only in `event_msg` records of type `agent_reasoning`.
- `session_id` is **not** a second copy of `id`. In a subagent's session it is
  the parent's id.
- `base_instructions` and `context_window` are **objects, not single values**,
  and Codex rejects the whole file if either shape is wrong.
