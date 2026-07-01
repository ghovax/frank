**Read a file or directory** from the local filesystem. If the path does not exist, an error is returned.

**Usage:**
- `file_path` should be an **absolute path** (a relative path is resolved against the working directory).
- By default returns up to **2000 lines** from the start of the file.
- `offset` is the **1-indexed** line number to start from; `limit` caps the number of lines returned.
- To read a later section, call again with a larger `offset`.
- Use **search_content** to find specific text in large files or files with long lines.
- If you are unsure of the path, use **find_files** to look it up by glob pattern.
- File contents come back with each line prefixed as `<line>: <content>`. Use those line numbers with **replace_lines** for targeted edits; do not include the `<line>:` prefixes in replacement text.
- File reads include a content hash used internally to reject stale edits if the file changes before **replace_lines** or **write_file** runs.
- Directories come back as an entry list (with a trailing `/` on subdirectories).
- Any line longer than **2000 characters** is truncated.
- **Call this tool in parallel** when you have several files to read.
- *Avoid tiny repeated slices* (30-line chunks) — if you need more context, read a larger window.

This tool is **read-only**. Always provide a concise *justification* that states why this read advances the task.
