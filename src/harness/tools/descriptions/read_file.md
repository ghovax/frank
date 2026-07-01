**Read a file** from the local filesystem. If the path does not exist, or points to a directory, an error is returned.

**Usage:**
- `file_path` should be an **absolute path**; a relative path is resolved against the working directory.
- By default reads up to **2000 lines** starting at the beginning of the file.
- `offset` is the **1-indexed** line number to start reading from; `limit` caps the number of lines returned. Use them for a large file that does not fit in one read; otherwise read the whole file by omitting them.
- Use **find_files** to locate files and **search_content** to locate matching lines before reading.
- Results come back in `cat -n` format: each line is prefixed with its right-aligned line number and a tab. When editing with **edit_file**, copy `old_string` from the content without that line-number/tab prefix.
- File reads include a content hash used internally to reject stale edits if the file changes before **edit_file** or **write_file** runs.
- Any line longer than **2000 characters** is truncated to prevent overflow.
- **Call this tool in parallel** when you have several known files or line ranges to read.
- Do not use this tool to read a folder. Use **find_files** for file discovery or `bash` for a deliberate directory inspection.

This tool is **read-only**. Always provide a concise *justification* that states why this read advances the task.
