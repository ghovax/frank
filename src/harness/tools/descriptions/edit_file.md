Performs **exact string replacements** in a file.

**Usage:**
- You **must** call **read_file** at least once in the conversation before editing — this tool errors otherwise.
- When copying text from read_file output, preserve the **exact indentation** (tabs/spaces) that appears *after* the `<line>: ` line-number prefix. Never include any part of the prefix itself in `old_string` or `new_string`.
- **Always prefer editing existing files.** Never write new files unless explicitly required.
- The edit **fails** if `old_string` is not found in the file.
- The edit **fails** if `old_string` matches multiple times and `replace_all` is `false` — provide more surrounding context to make the match unique, or set `replace_all` to replace every occurrence.
- Use `replace_all` to **rename** a string everywhere in the file (e.g. renaming a variable).
- When `old_string` is empty and the file does not exist, a **new file** is created with `new_string` as its contents.

This tool **modifies files**. Provide a concise *justification* and assess **risk**: `low` for a targeted edit, `medium` for a broad change, `high` for a destructive or hard-to-reverse change.
