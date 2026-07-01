**Apply a unified diff patch to one existing file.**

**Parameters:**
- `file_path` (`string`): Absolute path, or path relative to the working directory.
- `diff` (`string`): Unified diff hunk text for this file. It may include normal `diff --git`, `---`, and `+++` headers, but the `file_path` argument is the authority for which file is edited.
- `justification` (`string`): Concise user-facing reason for the edit.
- `risk` (`"low" | "medium" | "high"`): `low` for targeted edits, `medium` for broad edits, `high` for destructive or hard-to-reverse changes.

**Usage:**
- You **must** call **read_lines** before editing an existing file. The runtime records the file hash from that read and rejects the patch if the file changed afterward.
- Use normal unified diff hunks:

```diff
@@ -10,5 +10,6 @@
 context line
-old line
+new line
+another new line
 context line
```

- Hunk lines must start with a space for context, `-` for removed lines, or `+` for added lines.
- Prefer one `apply_patch` call per edited file. Put all hunks for that file in the same `diff` string.
- Use **write_file** for creating a new file or replacing an entire file from scratch.

This tool **modifies files**. Provide a concise *justification* and assess **risk**: `low` for a targeted edit, `medium` for a broad change, `high` for a destructive or hard-to-reverse change.
