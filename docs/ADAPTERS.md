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

**Populate `provenance` on cross-tool writes, and persist it.** If
`source_tool` differs from the tool being written into, `provenance` is
mandatory and `conversion_notes` must list every lossy conversion. Silently
passing a converted conversation off as native is a correctness bug.

Setting the field is only half of it, and the half that was done for months
while the other half was missing: call `ferry.core.provenance.record()` after
the write succeeds, and `recall()` on export. An adapter that sets the field
and stops has written to an object that is about to be discarded, and every
document promising provenance is then untrue.

Pass no `env` to either. An adapter's environment says where *its tool* keeps
things; Ferry's own directory is resolved from the process environment.

**Find the gate that comes after the write.** Three tools, three times: a
Copilot transcript is invisible without an entry in the workspace chat index, a
Claude Code conversation cannot be opened from an untrusted folder, and a Codex
rollout with no row in `threads` is offered by nothing. In each case the file
was complete and correct on disk and the tool's list was empty —
indistinguishable from the import having failed. **Locating that gate is part
of writing the adapter**, not a defect found afterwards, and it is the first
thing to go looking for in a tool that is new to Ferry.

**Write a value in the tool's own spelling, not merely in a correct one.** Codex's picker matches the working directory as a string, and it stores a Windows path in the extended-length form (`\\?\C:\...`). Ferry stored the same directory spelt plainly, and the picker matched nothing -- a row that was correct in every field and invisible anyway. Where a tool will compare a value rather than resolve it, copy the spelling it uses.

**Verify the way a person will use it.** The Codex path was checked with
`resume <id>`, which works with the index empty, so the check passed while the
picker showed nothing. A verification that takes a shortcut no user has proves
the shortcut.

**Write the record the interface reads, not only the one the model reads.** A
format may keep two accounts of the same turn — Codex keeps `response_item`
for the model and `event_msg` for the screen. Write one and the conversation
resumes with half of it missing.

**Fill a required field even when the source has nothing to put in it.** UCS
makes `Message.timestamp` optional because Copilot genuinely records none;
Codex requires a time on every record. The middle format is deliberately
permissive, so **each writer copes with what its own tool demands** rather than
trusting UCS to have supplied it. A field written only when the source happens
to carry it is a bug waiting for the first source that does not.

**Beware a format that records every turn twice.** Codex writes a canonical
record and an interface event for the same turn; reading both doubles the
conversation, and reading only the canonical one loses the parts it does not
carry. Census which side holds what before mapping either.

**A field that looks like a duplicate id may be a link.** Codex's `session_id`
equals `id` on an ordinary thread and holds the *parent's* id on a subagent
thread. Overwriting it on import reparented the subagent to itself — caught by
the round-trip test, not by any unit test that existed at the time.

**Carry the header, not the whole file.** When a tool validates a session
header strictly, guessing its schema means the tool rejects the entire
conversation, usually with no error. Keeping the real header verbatim in
`source_raw` costs a few tens of KB and removes the guess. That is often a
better trade than copying the whole transcript: for Codex it is 40 KB against
57 MB.

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
