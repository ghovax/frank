**Read selected lines from one file** on the local filesystem. If the path does not exist, or points to a directory, an error is returned.

**Usage:**
- `file_path` should be an **absolute path**; a relative path is resolved against the working directory.
- By default returns up to **2000 lines** starting at line 1.
- `start_line` is the **1-indexed** line number to start from; `line_count` caps the number of lines returned.
- Set `read_all=true` only when you already know the file is small enough to fit comfortably in context.
- Use **find_files** to locate files and **search_content** to locate matching lines before reading.
- File contents come back with each line prefixed as `<line>: <content>`. Use those line numbers with **replace_lines** for targeted edits; do not include the `<line>:` prefixes in replacement text.
- File reads include a content hash used internally to reject stale edits if the file changes before **replace_lines** or **write_file** runs.
- Any line longer than **2000 characters** is truncated.
- **Call this tool in parallel** when you have several known files or line ranges to read.
- Do not use this tool to read a folder. Use **find_files** for file discovery or `bash` for a deliberate directory inspection.

This tool is **read-only**. Always provide a concise *justification* that states why this read advances the task.
