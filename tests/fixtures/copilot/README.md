# Copilot Chat fixtures

Record shapes taken from real VS Code 1.134.0 transcripts; **all content is
invented**. No conversation text from any real session appears here.

Each file is a chat transcript as VS Code writes it: a `kind: 0` snapshot on the
first line, then one record per change. See `PROGRESS.md` §4.2 for the census
these were built from.

| File | What it covers |
|------|----------------|
| `basic.jsonl` | The ordinary case: an empty snapshot, turns arriving as appends, assistant text with **no `kind` field**, a tool call with its result. |
| `splice.jsonl` | A `kind: 2` record carrying `i`. The response is streamed, then revised — reading `i` as a plain append duplicates the answer. |
| `edge.jsonl` | An unknown delta kind, a corrupt line, an empty `thinking` block, a turn marked `hiddenFromTranscript`, and an unknown response kind. |
| `empty.jsonl` | A chat panel that was opened and never used. VS Code writes one of these every time; it is the common case, not a fault. |
| `uris.jsonl` | The file tools as they really arrive: `toolSpecificData` empty, and the files named only by `invocationMessage.uris`. Also a tool that carries arguments and no `uris`. |

Two properties these exist to protect:

- **Reading only the snapshot finds nothing.** Every real file on the probe
  machine had `requests: []` on line 1.
- **The assistant's prose has no discriminator.** It is a `MarkdownString`
  identified by having a `value` and no `kind`, and three different key sets for
  it appear in real data.
