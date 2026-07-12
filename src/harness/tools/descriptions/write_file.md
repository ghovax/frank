**Writes a file** to the filesystem of the location you target.

**Usage:**
- **Overwrites** the existing file if one exists at the path.
- Prefer **read_file** first before overwriting existing files when you need to preserve or inspect current content. If a prior read happened, the harness checks its content hash and rejects stale overwrites.
- **Always prefer editing existing files.** Never write new files unless explicitly required.
- **Never** proactively create documentation files (`*.md`, README) unless the user explicitly asks.
- Use this instead of `bash` with `echo >`, `cat <<EOF`, or `sed`/`awk` for creating or fully replacing a file. For *targeted* changes to an existing file, prefer **edit_file**.

This tool **modifies files**. Provide a concise *justification* (kept to one open-ended clause rather than a `label: detail` title) and assess **risk**: `low` for a new project file, `medium` for a broad rewrite, `high` for overwriting something hard to reconstruct.
