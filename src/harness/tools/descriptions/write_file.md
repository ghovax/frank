**Writes a file** to the local filesystem.

**Usage:**
- **Overwrites** the existing file if one exists at the path.
- If the file already exists, you **must** call **read_lines** first — this tool errors otherwise.
- **Always prefer editing existing files.** Never write new files unless explicitly required.
- **Never** proactively create documentation files (`*.md`, README) unless the user explicitly asks.
- Use this instead of `bash` with `echo >`, `cat <<EOF`, or `sed`/`awk` for creating or fully replacing a file. For *targeted* changes to an existing file, prefer **apply_patch**.

This tool **modifies files**. Provide a concise *justification* and assess **risk**: `low` for a new project file, `medium` for a broad rewrite, `high` for overwriting something hard to reconstruct.
