# Codex fixtures

Anonymised rollouts. **Every field name, type and nesting level is copied from
a census of 25,884 real records** written by Codex CLI 0.145–0.149 on Windows;
**every piece of content is invented.** No line here came out of a real
conversation.

| File | What it is for |
|---|---|
| `basic.jsonl` | An ordinary session, including the traps: encrypted `reasoning` beside readable `agent_reasoning`, and `event_msg` echoes of turns `response_item` already carries |
| `edge.jsonl` | An MCP result that duplicates a `response_item` output and one that exists nowhere else; an inline image; a subagent header whose `session_id` is the *parent* thread; unknown record, payload and content-block types; a malformed line; a bare array; the four record types with no `payload.type` |
| `empty.jsonl` | Metadata only, no messages. A session that must be skipped, not exported empty |

Three details these fixtures exist to pin down, each of which silently
destroys data if assumed rather than checked:

- `reasoning` carries `encrypted_content` and **no plaintext**; the readable
  text is only in `event_msg`/`agent_reasoning`
- `session_id` is **not** a second copy of `id` — on a subagent thread it is
  the parent's id
- `base_instructions` and `context_window` are **objects, not scalars**, and
  Codex rejects the whole file if either shape is wrong
