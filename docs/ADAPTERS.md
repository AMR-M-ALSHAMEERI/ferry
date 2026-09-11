# Writing an adapter

An adapter is the only code in Ferry that knows where a tool keeps its
conversations or what shape they are in. Everything else, the bundle, the
schema and the CLI, knows nothing about any particular tool, and stays that
way.

`ferry.adapters.claude_code` is the reference implementation. Read it alongside
this page: the rules below are the ones it follows, with the reasons.

## The contract

```python
class Adapter(ABC):
    name: str  # "claude-code"
    display_name: str  # "Claude Code"

    def detect(self) -> DetectResult: ...
    def export(self, dest_bundle_dir: Path) -> Iterator[ExportEvent]: ...
    def import_(self, bundle_dir: Path, options: ImportOptions) -> Iterator[ImportEvent]: ...

    # Folders the tool will refuse to open until it is told to allow them.
    def unopenable(self, bundle_dir: Path, options: ImportOptions) -> list[str]: ...

    # Taking back what an import wrote. Every default makes a delete do nothing.
    def written_roots(self) -> list[Path]: ...
    def listing(self, written: Path) -> Path | None: ...
    def unlist(self, written: Path) -> None: ...
    def in_use(self) -> str | None: ...
    def used_since(self, written: Path) -> bool: ...
    def only_opened(self, written: Path, was: Written) -> bool: ...
    def companions(self, written: Path) -> list[Path]: ...
```

Register the finished adapter in `ferry/adapters/__init__.py`, replacing its
placeholder. It cannot be registered in `base.py`, because every adapter
imports `base` and an import in the other direction would be circular.

Events carry a `kind` (`started`, `progress`, `skipped`, `note`, `warning`,
`error`, `done`), an optional `conversation_id`, and a message. The CLI shows
them and the tests check them. One `progress` event means exactly one
conversation handled, and the counts in the self-checks rely on that.

**Taking back an import.** A target also describes how to undo what its import
wrote, so *Delete conversations Ferry imported* and `ferry remove` can do it,
with `ferry.adapters.removal` doing the rest:

- `written_roots` says where conversations are written. A delete never follows
  a record anywhere outside them.
- `listing` and `unlist` name and remove the entry that makes the tool list a
  conversation, which is the other half of every import. `unlist` must do no
  harm when the entry is already gone, because a delete that stopped halfway is
  finished by running it again.
- `in_use` explains why the tool has to be closed first.
- `used_since` reports use the checksum cannot see, such as a SQLite journal
  beside the file.
- `only_opened` proves, when the checksum no longer matches, that the only
  change is the tool having opened the file. Without that proof, a changed file
  belongs to the person.
- `companions` lists files that belong to the conversation and go with it.

Record provenance with `title=` so the delete screen can name what it offers.

## Rules

**`detect()` must never raise.** It runs at startup for every registered tool.
A tool that is not installed, a folder that cannot be read, a home directory
that cannot be found: all of them return `installed=False` with the reason in
`notes`. One broken check must not stop someone migrating the others.

**`export()` must never write to the source.** Not a lock file, not a marker,
not a changed timestamp. Every adapter's self-check takes a checksum of the
tool's whole data folder before and after, and fails if a single byte moved.
This is the most important safety property in the project.

**`export()` must be resumable.** Running it again after an interruption skips
what is already in the bundle. Key it on something stable: the Claude Code
adapter uses the session id as the conversation id, so exporting again changes
nothing rather than piling up duplicates. Write attachments and `source_raw`
*before* the conversation file, so that the conversation file existing really
does mean "finished".

**Never make anything up.** A timestamp the source did not store is `null`, not
`now()`. A conversation with no messages is skipped, not exported empty. A
count you cannot determine is not a guess.

**Bad input costs a line, not a conversation.** Unknown record types, unknown
content blocks, JSON that will not parse, missing fields: warn and move on. A
reader that stops at the first surprise loses everything after it, and these
formats change between versions without notice.

**Carry the original bytes.** The conversation schema is a common denominator,
so it loses detail by design. `source_raw/<uuid>.bin` is the copy that does not
go through it. With it, restoring into the same tool replays the original file
with only the machine specific fields rewritten, and comes back byte for byte.
Without it, records are rebuilt from the schema, which works but loses anything
the schema has no field for. Say what was lost. Never hide it.

**Rewrite references, never content.** Absolute paths in structural fields,
such as the working directory or pointers to files stored beside the
conversation, are references the tool will follow, and they must be remapped. A
path that appears inside a message is something a person or an assistant
*said*, and Ferry does not edit that.

**Work out target paths from the target.** If a tool encodes the working
directory into a folder name, that encoding usually loses information.
Calculate the new name from the new working directory, and never try to decode
the old one.

**Fill in `provenance` on every conversion, and keep it.** If `source_tool`
differs from the tool being written into, `provenance` is required and
`conversion_notes` must list everything lost. Quietly passing a converted
conversation off as native is a bug.

Setting the field is only half the job, and for months it was the only half
done. Call `ferry.core.provenance.record()` after the write succeeds, and
`recall()` when exporting. An adapter that sets the field and stops has written
to an object about to be thrown away, which makes every page promising
provenance untrue.

Pass no `env` to either call. An adapter's environment says where *its tool*
keeps things. Ferry's own folder comes from the process environment.

**Find what comes after the write.** It happened with three tools: a Copilot
transcript is invisible without an entry in the workspace chat index, a Claude
Code conversation cannot be opened from a folder the tool does not trust, and a
Codex session with no row in its sessions table is offered by nothing. Each
time, the file was complete and correct and the tool's list was empty, which
looks exactly like a failed import. **Finding that step is part of writing the
adapter,** not a defect discovered later, and it is the first thing to look for
in a tool new to Ferry.

**Write a value the way the tool spells it, not just correctly.** Codex's
picker compares the working directory as a string, and it stores a Windows
path in the extended form, `\\?\C:\...`. Ferry stored the same folder written
plainly, and the picker matched nothing: a row correct in every field, and
invisible anyway. Where a tool compares a value rather than resolving it, copy
its spelling.

**Check it the way a person will use it.** The Codex path was first checked
with `resume <id>`, which works with the sessions table empty, so the check
passed while the picker showed nothing. A check that takes a shortcut no user
takes only proves the shortcut.

**Write what the screen reads, not only what the model reads.** A format can
keep two accounts of the same turn. Codex keeps `response_item` records for the
model and `event_msg` records for the screen. Write only one and the
conversation resumes with half of it missing.

**Fill a required field even when the source has nothing for it.** The schema
makes a message's timestamp optional, because Copilot records none. Codex needs
a time on every record. The shared schema is deliberately relaxed, so **each
writer meets its own tool's demands** instead of trusting the schema to have
supplied the value. A field written only when the source happens to have it is
a bug waiting for the first source that does not.

**Watch for a format that records every turn twice.** Codex writes a record for
the model and an event for the screen for the same turn. Reading both doubles
the conversation, and reading only the first loses what the second alone holds.
Count which side holds what before mapping either.

**A field that looks like a duplicate id may be a link.** Codex's `session_id`
equals `id` in an ordinary session and holds the *parent's* id in a subagent's.
Overwriting it on import made a subagent its own parent, which the round trip
test caught and no unit test at the time did.

**Carry the header, not the whole file.** When a tool checks a session header
strictly, guessing its layout means the tool rejects the whole conversation,
usually without an error. Keeping the real header in `source_raw` costs a few
tens of kilobytes and removes the guess. That is often better than copying the
whole transcript: for Codex it is 40 KB against 57 MB.

## Studying a new tool

Do this before writing any adapter code, and record what you find in
`docs/FORMATS.md`:

1. Find where the data lives, and every environment variable that can move it.
2. Count the record types and their field names across **real** data, not the
   vendor's documentation and not a single sample. Every Claude Code assumption
   that turned out wrong would have looked fine in one sample.
3. Count the content block types and the field each one keeps its content in.
   A `thinking` block stores its text under `thinking`, so assuming `text`
   empties every reasoning block while the block count still looks right.
4. Look for companion files referred to by absolute path. They are easy to miss
   and leave broken references after an import.
5. Find whatever makes a conversation *visible* in the tool. Some tools need an
   index entry as well as the transcript. Without it the import succeeds and the
   person sees nothing.
6. If the tool encodes paths into names, check what it does with a path full of
   punctuation.

## Tests

Fixtures go in `tests/fixtures/<tool>/`, with **real record shapes and made up
content**: anonymised, never a real conversation. A fixture produced by the
exporter only proves that the exporter agrees with itself.

Check import results with an independent parser rather than the adapter's own
reader, so a matching pair of bugs cannot cancel out. Write expected paths out
in full rather than calling the function under test to produce them.

Every adapter needs at least a round trip (export, import, export again, and
compare), a check that the source was not modified, one test for each kind of
bad input, and the path remap.
