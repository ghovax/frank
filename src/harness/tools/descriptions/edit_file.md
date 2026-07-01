**Replace an exact substring in one existing file.**

**Parameters:**
- `file_path` (`string`): Absolute path, or path relative to the working directory.
- `old_string` (`string`): The exact text to replace, copied verbatim from the file.
- `new_string` (`string`): The text to replace it with. Must differ from `old_string`.
- `replace_all` (`boolean`): Replace every occurrence instead of requiring a unique match. Defaults to `false`.
- `justification` (`string`): Concise user-facing reason for the edit.
- `risk` (`"low" | "medium" | "high"`): `low` for targeted edits, `medium` for broad edits, `high` for destructive or hard-to-reverse changes.

**Usage:**
- You **must** call **read_file** before editing an existing file. The runtime records the file hash from that read and rejects the edit if the file changed afterward.
- `old_string` is matched **character-for-character** against the file. Copy it straight from the `read_file` output, excluding the line-number/tab prefix. Whitespace, indentation, and quote style must match exactly — a curly quote in the file must be a curly quote in `old_string`; a straight quote you type will not match.
- By default `old_string` must be **unique** in the file. If it appears more than once the edit is rejected — add surrounding context to make it unique, or set `replace_all=true` to replace every occurrence.
- The edit fails if `old_string` is not found, or if `old_string` and `new_string` are identical.
- Prefer one **edit_file** call per distinct change. To make several edits to the same file, issue them as separate calls (each against the file's current state).
- Use **write_file** for creating a new file or replacing an entire file from scratch.

This tool **modifies files**. Provide a concise *justification* and assess **risk**: `low` for a targeted edit, `medium` for a broad change, `high` for a destructive or hard-to-reverse change.
