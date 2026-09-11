# Claude Code fixtures

Anonymised transcripts. **Every field name, type and level of nesting is copied
from a count of 4,238 real records** written by Claude Code 2.1.229 to 2.1.237
on Windows. **Every piece of content is invented.** No line here came from a
real conversation.

That split is deliberate. Ferry's tests never use fixtures produced by the code
under test, because a fixture the exporter wrote only proves that the exporter
agrees with itself. What makes these fixtures real is their *shape*, which is
exactly what a reader can get wrong. What makes them safe to commit is that
their content is not real.

| File | What it is for |
|---|---|
| `basic.jsonl` | An ordinary session, with every record type and every content block type in the proportions the real data had |
| `edge.jsonl` | The things that break a careless reader: plain string content, an inline image, tool output stored apart, unknown record and block types, a malformed line, a blank line and a bare array |
| `empty.jsonl` | Metadata records only, with no messages. A session that must be skipped rather than exported empty |

The field names come from the count recorded in `docs/FORMATS.md`. In
particular, `thinking` keeps its text under `thinking` rather than `text`,
`tool_result` keeps its payload under `content` rather than `output`, and
`tool_use` has an `id`.
