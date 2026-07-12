**Replace exact text in a file.** The edit is staged in memory, validated against a language parser (AST for Python, tree-sitter for most others), and committed only if the result is syntactically valid.

**Parameters:**
- `file_path` (`string`): Absolute path, or path relative to the working directory.
- `find` (`string`): The exact text to replace, copied verbatim from the file.
- `replace_with` (`string`): The text to replace it with.
- `location` (`string`): The project location to edit in — its URI or name from your context. Defaults to the local filesystem; pass it only to edit on a different (remote) location.
- `replace_all` (`boolean`): Replace every occurrence instead of requiring a unique match. Defaults to `false`.
- `justification` (`string`): Concise user-facing reason for the edit, as a single smooth open-ended clause rather than a `label: detail` title.
- `risk` (`"low" | "medium" | "high"`): `low` for targeted edits, `medium` for broad changes, `high` for destructive or hard-to-reverse changes.

**Usage:**
- Prefer **read_file** before editing when you need exact surrounding text or line-number context. If you already have the exact `find` text from another reliable source, you may edit without a prior read.
- When a prior **read_file** happened, the harness records the file's content hash and rejects stale edits if the file changed externally.
- `find` must appear **exactly once** in the file (or set `replace_all=true`). Copy it character-for-character from the `read_file` output, excluding the line-number/tab prefix. Whitespace, indentation, and quote style must match exactly.
- If an exact match fails, the harness retries with trailing whitespace stripped from every line of both the `find` text and the file content. This covers common whitespace mismatches.
- `find` and `replace_with` are plain strings with literal newlines. Write the code exactly as it should appear — code that legitimately contains escape sequences (a `"\n"` inside a string literal, a `\w+` in a regex) is matched exactly as written.

**Validation:** The harness runs the result through a syntax parser before writing:
- **Python** (`.py`): stdlib `ast` — produces exact error messages with line and column.
- **JS/TS/JSON/YAML/TOML/HTML/CSS/Go/Rust/C/C++/Bash** (`.js`, `.ts`, `.tsx`, `.json`, `.yaml`, `.toml`, `.sh`, `.html`, `.css`, `.go`, `.rs`, `.c`, `.cpp`, …): tree-sitter — catches syntax errors with error-text snippets.
- **Unknown extensions**: validation is skipped (file commits directly).

If validation fails:
- **The file on disk is NOT modified.**
- A structured diagnostic is returned with the error line, column, message, and a context snapshot of the *prospective* (broken) state.
- Issue a corrective `edit_file` call to fix the error. Do **not** call `read_file` — the disk still has the old correct state.

**Examples:**

**Rename a variable.** Copy the exact line from `read_file` output.
```json
{
  "file_path": "src/main.py",
  "find": "x = calculate_total(items, tax_rate)",
  "replace_with": "total = calculate_total(items, tax_rate)"
}
```

**Rewrite a function.** Multi-line text with literal newlines.
```json
{
  "file_path": "src/main.py",
  "find": "def old_method():\n    return 1",
  "replace_with": "def new_method():\n    return 42"
}
```

**Replace every occurrence of a class name.** Use `replace_all=true` when the same text appears in multiple places.
```json
{
  "file_path": "src/app.ts",
  "find": "bg-red-500",
  "replace_with": "bg-blue-500",
  "replace_all": true
}
```

This tool **modifies files**. Provide a concise *justification* (phrased as a smooth open-ended sentence, not a `label: detail` heading) and assess **risk**: `low` for a targeted edit, `medium` for a broad change, `high` for a destructive or hard-to-reverse change.
