# Changelog

Every release of Ferry, newest first.

## 0.1.0 (unreleased)

The first release.

### Backing up and restoring

- Export the conversation history of Claude Code, OpenAI Codex, GitHub Copilot
  Chat and Google Antigravity into a bundle: an ordinary folder you can copy
  anywhere.
- Import a bundle back into the same assistant, on the same machine or a new
  one. When the bundle was made under a different home folder, Ferry asks where
  those folders live now.
- Seal a bundle into one encrypted `.ferry` file, with a passphrase that is
  never stored.
- Look inside a bundle, and delete a conversation from it or the whole bundle.

### Moving between assistants

- Convert conversations from any of the four assistants into any other, in
  one of two ways: keep the most detail for reading, or give up the private
  reasoning and tool output so the conversation can be carried on.
- Every conversion is counted and explained before anything is written, and
  recorded as converted afterwards.
- Take back a conversation Ferry imported, as long as nobody has worked in it
  since.

### Everything else

- An interactive menu that finds your assistants first, in four themes.
- Commands for scripts: `ferry export`, `ferry import`, `ferry remove`,
  `ferry compact`, `ferry tools` and `ferry skill`.
- Compact: turn one conversation into a short document to start a new session
  with, offline and without an account or a model.
- A skill file that teaches an AI coding assistant to use Ferry safely, and
  `ferry skill --install` to add it to Claude Code, OpenAI Codex, GitHub
  Copilot Chat and Google Antigravity.
- Automatic backups before any import replaces or any delete removes, and a
  screen to clean them up.
