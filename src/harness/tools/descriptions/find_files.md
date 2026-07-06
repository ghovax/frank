**Fast file-pattern matching** that works with any codebase size.

- Supports glob patterns like `**/*.js` or `src/**/*.ts`.
- Returns matching file paths, sorted by **modification time** (most recent first).
- Use this when you need to find files **by name pattern**.
- For open-ended searches that may need multiple rounds, use **spawn_agent** (the Task tool) instead.
- You can call multiple tools in a single response — **batch several searches** at once when each might be useful.
- **Prefer this** over `bash` with `find` or `ls` for filename lookups.

This tool is **read-only**. Always provide a concise *justification* that states why this search advances the task — keep it a smooth open-ended clause, never a `label: detail` heading.
