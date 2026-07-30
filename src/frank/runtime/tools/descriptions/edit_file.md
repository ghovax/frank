Replace exact text in a file, staged and validated before commit.

``find`` must occur verbatim in the file. Unless ``replace_all`` is set, it must be unique. Copy it character-for-character from ``read_file`` without its line-number prefix. A prior read supplies a content hash so stale edits are rejected if the file changes externally.

The prospective result is syntax-checked before writing: Python uses its AST and supported languages use tree-sitter. On validation failure, the file on disk remains unchanged and the returned diagnostic describes the prospective broken state; correct the edit without rereading unchanged disk content.

Arguments:
  - file_path: Absolute path (or path relative to the working directory).
  - find: The exact text to find, copied verbatim from the file.
  - replace_with: The text to replace it with.
  - location: The workspace location to edit in — its URI or name from the locations listed in your context. Defaults to the local filesystem; pass it only to target a different (remote) location.
  - replace_all: Replace every occurrence instead of requiring a unique match.
  - explanation: A concise, user-facing reason for this edit.
  - risk: "low" for targeted edits, "medium" broad, "high" hard to reverse.
