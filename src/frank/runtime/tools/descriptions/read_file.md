Read a file, returning its lines in cat -n format. Image files (.png/.jpg/.jpeg/.gif/.webp) are ingested natively instead: the result is structured metadata, and on a vision model the image itself follows.

Text lines carry 1-indexed line numbers for orientation. Exclude that prefix when copying exact text into ``edit_file``. Large files can be read in windows with ``offset`` and ``limit``; lines over the inline ceiling are reported as truncated and must not be copied into an exact-match edit. Reads record a content hash so later edits can reject stale state. Use ``search_code`` to find code by meaning, and ``bash`` with ripgrep/fd for exact names or content; do not use this on a directory. Batch independent file reads in one response.

Arguments:
  - file_path: Absolute path (or path relative to the working directory).
  - location: The project location to read from — its URI or name from the locations listed in your context. Defaults to the local filesystem; pass it only to target a different (remote) location.
  - offset: 1-indexed line number to start reading from.
  - limit: Maximum number of lines to return (defaults to 2048).
  - explanation: A concise, user-facing reason for this read.
