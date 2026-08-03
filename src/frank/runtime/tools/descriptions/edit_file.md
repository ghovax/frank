Replace exact text in a file. The harness stages the edit and validates it before it writes.

`find` must appear in the file word for word. It must also be unique, unless you set `replace_all`. Copy it character for character from `read_file`, without the line-number prefix. An earlier read supplies a content hash, so the harness rejects a stale edit if the file changed outside this session.

The harness syntax-checks the prospective result before it writes. Python uses its own AST, and the supported languages use tree-sitter. Where validation fails, the file on disk does not change, and the diagnostic describes the state the edit would have produced. Correct the edit. Do not read unchanged content again.

Arguments:
  - file_path: An absolute path, or a path relative to the working directory.
  - find: The exact text to find, copied word for word from the file.
  - replace_with: The text that replaces it.
  - location: Which workspace location holds the file — its URI or its name, from the locations in your context. Defaults to the local filesystem. Pass it only to reach a different, remote location.
  - replace_all: Replace every occurrence, instead of requiring one unique match.
  - explanation: A short reason for the edit, in the words the user reads.
  - risk: "low" for a targeted edit, "medium" for a broad one, "high" for one that is hard to reverse.
