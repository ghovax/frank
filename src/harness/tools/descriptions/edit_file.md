**Edit a file using coordinate-based operations.** Targets lines by their 1-indexed number from `read_file` output. Never echo existing code — only supply the new content.

**Parameters:**
- `file_path` (`string`): Absolute path, or path relative to the working directory.
- `operations` (`list[dict]`): One or more operation dicts. Each has a `type` field and type-specific coordinate fields. `text` is a plain string with literal newlines (no escaping or array-of-lines syntax).
- `skip_validation` (`boolean`): Skip AST/syntax validation before writing. Defaults to `false`. Set to `true` when deliberately generating invalid syntax (e.g. scaffolding a broken file to fill in later).
- `justification` (`string`): Concise user-facing reason for the edit.
- `risk` (`"low" | "medium" | "high"`): `low` for targeted edits, `medium` for broad changes, `high` for destructive or hard-to-reverse changes.

**Operation types:**

| Type | Coordinates | `text` | What it does |
|---|---|---|---|
| `insert` | `start_line` | Required | Insert new lines **before** `start_line`. Use `start_line=1` to prepend, or `start_line=N+1` to append after the last line. |
| `delete` | `start_line`, `end_line` | Omitted | Remove lines from `start_line` to `end_line` (inclusive). |
| `replace_range` | `start_line`, `end_line` | Required | Atomically replace lines `[start_line, end_line]` with new lines. |
| `replace_text` | `start_line`, `end_line` | Uses `find`/`replace` | Within the bounded line range, replace each occurrence of `find` with `replace`. Like search-and-replace scoped to specific lines — solves mid-line changes without knowing the exact column. |
| `columnar_insert` | `start_line`, `end_line`, `column` | Required | Insert `text` at character offset `column` (0-indexed) on **every line** in the range. Lines shorter than `column` are padded with spaces. |
| `columnar_delete` | `start_line`, `end_line`, `column`, `length` | Omitted | Delete `length` characters at character offset `column` (0-indexed) on **every line** in the range. |

**Usage:**
- You **must** call **read_file** before editing. The harness records the file's content hash and rejects stale coordinates if the file changed externally.
- Line numbers are **1-indexed**, matching the numbers shown in `read_file` output. Read the relevant section of the file first to see its current line numbers.
- `text` is a **plain string with literal newlines**. Write the code exactly as it should appear — no escaping, no `\n` sequences, no array of lines.

**Multiple operations in one call:** Pass several operation dicts to edit multiple places in the same file atomically. Operations are executed bottom-to-top (by `start_line`) so coordinates stay stable. To edit *different* files, make separate `edit_file` calls.

**Validation:** After staging all operations in memory, the harness runs the result through a syntax parser (Python `.py` files via `ast`). If validation fails:
- **The file on disk is NOT modified.**
- A structured diagnostic is returned with the error line, column, message, and a context snapshot of the *prospective* (broken) state.
- Submit a corrective `edit_file` to fix the error. Do **not** call `read_file` — the disk still has the old correct state.

**Examples:**

Each example shows the JSON dict you put in the `operations` list. `text` is a plain string with literal newlines.

**Insert new code before a specific line.** Use when adding a function, a test, or a config entry. Never echo the lines around it — just the new content.

Line 10 in the original file is undisturbed; everything at 10 or below shifts down.

```json
{
  "type": "insert",
  "start_line": 10,
  "text": "def new_function():\n    return 42"
}
```

**Delete a range of lines.** Use to remove deprecated code, dead imports, or logged statements. Two numbers — no content echo, no token waste.

The model never reads or echoes the deleted content — just the line range.

```json
{
  "type": "delete",
  "start_line": 20,
  "end_line": 60
}
```

**Replace a range atomically.** Use when rewriting a function body, an import section, or a config block. The old lines are removed and new ones inserted in a single step.

The old content at those lines is deleted; the new text takes their place.

```json
{
  "type": "replace_range",
  "start_line": 12,
  "end_line": 14,
  "text": "def better():\n    return True"
}
```

**Comment out a block with columnar insert.** Use to temporarily disable code, toggle flags in config, or add annotations. The model never reads or echoes the line content.

Each line in the range gets `// ` inserted at column 0. Lines shift right; lines outside the range are untouched.

```json
{
  "type": "columnar_insert",
  "start_line": 40,
  "end_line": 80,
  "column": 0,
  "text": "// "
}
```

**Uncomment a block with columnar delete.** Use to re-enable disabled code or strip formatting prefixes.

Removes 3 characters at column 0 on every line from 40 to 80 — strips `// ` if present.

```json
{
  "type": "columnar_delete",
  "start_line": 40,
  "end_line": 80,
  "column": 0,
  "length": 3
}
```

**Find and replace text within a bounded line range.** Use for mid-line changes where you don't know the exact column — renaming a CSS class in a `className` string, swapping a function argument, updating a variable name. The line range eliminates ambiguity about which occurrence to change.

Scoped to lines 10 through 12. All occurrences of the `find` text within those lines are replaced with `replace`.

```json
{
  "type": "replace_text",
  "start_line": 10,
  "end_line": 12,
  "find": "bg-red-500",
  "replace": "bg-blue-500"
}
```

This tool **modifies files**. Provide a concise *justification* and assess **risk**: `low` for a targeted edit, `medium` for a broad change, `high` for a destructive or hard-to-reverse change.
