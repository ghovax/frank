**Read a file** from the filesystem of the location you target. If the path does not exist, or points to a directory, an error is returned.

**Images are ingested natively.** Reading a `.png` / `.jpg` / `.jpeg` / `.gif` / `.webp` file returns structured metadata (mime type, pixel dimensions, byte size) instead of text — and when your model supports vision, the image itself arrives in the conversation right after the result, so you can look at it directly. No base64 juggling, no shell tricks: just read the file. (On a text-only model, or for an image over the inline size ceiling, you get the metadata only.)

**Usage:**
- `file_path` should be an **absolute path**; a relative path is resolved against the working directory.
- By default reads up to **2048 lines** starting at the beginning of the file.
- `offset` is the **1-indexed** line number to start reading from; `limit` caps the number of lines returned. Use them for a large file that does not fit in one read; otherwise read the whole file by omitting them.
- Use `find_files` to locate files and `search_content` to locate matching lines before reading.
- Results come back in `cat -n` format: each line is prefixed with its right-aligned **1-indexed line number** and a tab. Use the line numbers to orient yourself, but when you copy text for an **edit_file** `find`, copy only the line content — exclude the line-number/tab prefix.
- File reads include a content hash used internally to reject stale edits if the file changes before **edit_file** or **write_file** runs.
- Any line longer than **2048 characters** is cut at that length; the result's `long_lines_truncated` field lists the affected line numbers. **Never copy a truncated line into an `edit_file` `find`** — its tail is missing, so the match would fail.
- **Call this tool in parallel** (several calls in one response) when you have several known files or line ranges to read.
- Do not use this tool to read a folder. Use `find_files` for file discovery or `bash` for a deliberate directory inspection.

This tool is **read-only**. A short *justification* is welcome when the purpose is not obvious from the path — a single flowing open-ended clause, never a `label: detail` heading.
