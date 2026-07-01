Replaces an inclusive range of lines in an existing file.

**Parameters:**
- `file_path` (`string`): Absolute path, or path relative to the working directory.
- `start_line` (`integer`): First 1-indexed line to replace.
- `end_line` (`integer`): Last 1-indexed line to replace. Use `start_line - 1` to insert before `start_line`.
- `new_lines` (`string[]`): Replacement lines, one string per line, with no newline characters and no `<line>:` prefixes. Use `[]` to delete the selected range.
- `justification` (`string`): Concise user-facing reason for the edit.
- `risk` (`"low" | "medium" | "high"`): `low` for a targeted edit, `medium` for a broad edit, `high` for destructive or hard-to-reverse changes.

**Usage:**
- You **must** call **read_file** before editing. The runtime records the file hash from that read and rejects the edit if the file changed afterward.
- Line numbers are **1-indexed** and refer to the `<line>:` prefixes shown by `read_file`.
- `start_line` is the first line to replace.
- `end_line` is the last line to replace.
- To insert before a line, set `end_line` to `start_line - 1`.
- To delete lines, pass an empty `new_lines` list.
- `new_lines` is a list with **one string per replacement line**. Do not include newline characters inside the strings, and do not include the `<line>:` prefixes.
- After a successful edit, call `read_file` again before the next `replace_lines` call for the same file, because line numbers may have shifted.
- Use **write_file** for creating a new file or replacing an entire file from scratch.

This tool **modifies files**. Provide a concise *justification* and assess **risk**: `low` for a targeted edit, `medium` for a broad change, `high` for a destructive or hard-to-reverse change.
