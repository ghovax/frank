Read a file. The lines come back in `cat -n` format.

An image file — `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp` — is ingested instead. The result is structured metadata, and on a vision model the image itself follows.

Each text line carries a 1-indexed line number, to orient you. Leave that prefix out when you copy exact text into `edit_file`.

Read a large file in windows with `offset` and `limit`. A line above the inline ceiling comes back marked as truncated, and you must not copy such a line into an exact-match edit.

Each read records a content hash, so a later edit can reject stale state. Use `search_code` to find code by meaning, and `bash` with ripgrep or `fd` for an exact name or an exact string. Do not point this tool at a directory. Batch independent reads into one response.

Arguments:
  - file_path: An absolute path, or a path relative to the working directory.
  - location: Which workspace location holds the file — its URI or its name, from the locations in your context. Defaults to the local filesystem. Pass it only to reach a different, remote location.
  - offset: The 1-indexed line to start from.
  - limit: How many lines to return. Defaults to 2048.
  - explanation: A short reason for the read, in the words the user reads.
