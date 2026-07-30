Write content to a file, overwriting it if it exists.

Prefer ``edit_file`` for a targeted change to an existing file. Read an existing file first when its current content must be preserved; the recorded hash lets the harness reject a stale overwrite. Do not create documentation files proactively unless the user asked for them. This tool modifies files.

Arguments:
  - file_path: Absolute path (or path relative to the working directory).
  - content: The full text to write to the file.
  - location: The workspace location to write to — its URI or name from the locations listed in your context. Defaults to the local filesystem; pass it only to target a different (remote) location.
  - explanation: A concise, user-facing reason for this write.
  - risk: "low" new file, "medium" broad rewrite, "high" hard to reconstruct.
