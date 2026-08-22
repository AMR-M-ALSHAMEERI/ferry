# Writing an adapter

An adapter is the only code in Ferry that knows where a tool keeps its
conversations or what shape they are in. Everything else — the bundle, the
schema, the CLI — is tool-agnostic and stays that way.

`ferry.adapters.claude_code` is the reference implementation. Read it alongside
this page; the rules below are the ones it follows and the reasons it follows
them.

## The contract

```python
class Adapter(ABC):
    name: str  # "claude-code"
    display_name: str  # "Claude Code"

    def detect(self) -> DetectResult: ...
    def export(self, dest_bundle_dir: Path) -> Iterator[ExportEvent]: ...
    def import_(self, bundle_dir: Path, options: ImportOptions) -> Iterator[ImportEvent]: ...
```

Register the finished adapter in `ferry/adapters/__init__.py`, replacing its
stub. Registration does not go in `base.py` — every adapter imports `base`, so
an import in the other direction is a cycle.

Events carry a `kind` (`started`, `progress`, `skipped`, `warning`, `error`,
`done`), an optional `conversation_id`, and a message. The CLI renders them;
tests assert on them. One `progress` event means one conversation actually
handled — the counts in the self-check depend on that.

## Rules

**`detect()` must never raise.** It runs at startup for every registered tool.
A tool that is not installed, a directory that cannot be read, a home directory
that cannot be resolved — all of them return `installed=False` with the reason
in `notes`. One broken probe must not stop the user migrating the others.

**`export()` must never write to the source.** Not a lock file, not a marker,
not a mtime touch. Every adapter self-check checksums the tool's whole data
directory before and after and fails if a byte moved. This is the single most
important safety property in the project.

**`export()` must be resumable.** Re-running after an interruption skips what
is already in the bundle. Key it on something stable — the Claude Code adapter
uses the session UUID as the conversation id, so a re-export is idempotent
rather than a pile of duplicates. Write attachments and `source_raw` *before*
the UCS file, so the UCS file's presence genuinely means "finished".

**Never fabricate.** A timestamp the source did not store is `null`, not
`now()`. A conversation with no messages is skipped, not exported empty. A
count you cannot determine is not an estimate you made up.

**Malformed input costs a line, not a conversation.** Unknown record types,
unknown content blocks, unparseable JSON, missing fields — warn and skip.
A reader that raises on the first surprise loses everything after it, and
formats change without warning between versions of these tools.

**Carry the original bytes.** UCS is a lossy common denominator by
construction; `source_raw/<uuid>.bin` is the copy that does not go through it.
With it, a same-tool import replays the original file with only the
machine-specific fields rewritten, and comes back byte-for-byte. Without it,
records are rebuilt from UCS — which works, but loses everything UCS has no
field for. Say what was lost; never absorb it.

**Rewrite references, never content.** Absolute paths in structural fields
(`cwd`, pointers to sidecar files) are references the tool will follow and must
be remapped. A path that appears inside a message is something a person or an
assistant *said*. Ferry does not edit that.

**Derive target paths from the target.** If a tool encodes the working
directory into a filename, that encoding is usually lossy. Compute the new name
from the new working directory; never try to decode the old one.

**Populate `provenance` on cross-tool writes.** If `source_tool` differs from
the tool being written into, `provenance` is mandatory and `conversion_notes`
must list every lossy conversion. Silently passing a converted conversation off
as native is a correctness bug.

## Probing a new tool

Do this before writing any adapter code, and record the findings in
`docs/FORMATS.md`:

1. Find the data root, and every environment variable that can move it.
2. Census the record types and their field names over **real** data — not the
   vendor's docs, and not one sample. Every Claude Code assumption that turned
   out wrong was wrong in a way one sample would not have shown.
3. Census the content-block types and the field each one carries its payload
   in. `thinking` stores its text under `thinking`; assuming `text` empties
   every reasoning block while the block count still matches.
4. Look for companion files referenced by absolute path. They are easy to miss
   and produce dangling references on import.
5. Find whatever makes a conversation *visible* in the tool's UI. Some tools
   need an index row written as well as the transcript; without it the import
   succeeds and the user sees nothing.
6. Check what the tool does with a path full of punctuation, if it encodes
   paths into names.

## Tests

Fixtures go in `tests/fixtures/<tool>/`, with **real record shapes and invented
content** — anonymised, never a real conversation. A fixture produced by the
exporter proves only that the exporter is self-consistent.

Assert import results with an independent parser rather than the adapter's own
reader, so a matched pair of bugs cannot cancel out. Assert path-derivation
expectations as literals rather than by calling the function under test.

Every adapter needs, at minimum: a round trip (export → import → export, equal),
a source-unmodified check, each malformed-input path, and the path remap.
