# Copilot Chat fixtures

Record shapes taken from real VS Code 1.134.0 transcripts. **All content is
invented.** No text from any real conversation appears here.

Each file is a chat transcript as VS Code writes it: a `kind: 0` snapshot on the
first line, then one record per change. `docs/FORMATS.md` describes the real
files these were built from.

| File | What it covers |
|---|---|
| `basic.jsonl` | The ordinary case: an empty snapshot, turns arriving as appends, assistant text with **no `kind` field**, and a tool call with its result. |
| `splice.jsonl` | A `kind: 2` record carrying `i`. The response is streamed and then revised, and reading `i` as a plain append duplicates the answer. |
| `edge.jsonl` | An unknown change kind, a corrupt line, an empty `thinking` block, a turn marked `hiddenFromTranscript`, and an unknown response kind. |
| `empty.jsonl` | A chat panel that was opened and never used. VS Code writes one of these every time, so it is the common case rather than a fault. |
| `uris.jsonl` | The file tools as they really arrive: `toolSpecificData` empty and the files named only in `invocationMessage.uris`. Also a tool that carries arguments and no `uris`. |

Two properties these fixtures exist to protect:

- **Reading only the snapshot finds nothing.** Every real file studied had
  `requests: []` on its first line.
- **The assistant's text has no label.** It is a `MarkdownString`, recognised
  by having a `value` and no `kind`, and three different sets of keys for it
  appear in real data.
