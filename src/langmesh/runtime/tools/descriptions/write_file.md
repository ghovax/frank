Write content to a file. This overwrites the file where one exists.

Prefer `edit_file` for a targeted change to a file that exists. Read a file first where its current content must survive; the recorded hash then lets the harness reject a stale overwrite.

Do not create a documentation file unless the user asked for one. This tool changes files.

Arguments:
  - file_path: An absolute path, or a path relative to the working directory.
  - content: The full text to write.
  - location: Which workspace location receives the file — its URI or its name, from the locations in your context. Defaults to the local filesystem. Pass it only to reach a different, remote location.
  - explanation: A short reason for the write, in the words the user reads.
