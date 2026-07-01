**Fast content search** that works with any codebase size.

- Searches file contents using **regular expressions** (e.g. `log.*Error`, `function\s+\w+`).
- Filter by file pattern with `include` (e.g. `*.py`, `*.{ts,tsx}`).
- Returns **file paths and line numbers** with the matching lines.
- If you need the *exact count* of matches within files, use `bash` with `rg` (ripgrep) directly — do **not** use this tool merely to count.
- For open-ended searches that may need multiple rounds, use **spawn_agent** instead.
- **Prefer this** over `bash` with `grep` or `rg` for content lookups.
- Never search the real home directory (`~` or `/Users/<name>`). Pass a project path, known subdirectory, or specific file in `path`.

This tool is **read-only**. Always provide a concise *justification* that states why this search advances the task.
