The user just approved this shell command and chose to **always allow** commands like it for the rest of this session:

{{ command }}

Produce the allow rule: one or more command patterns that should auto-run without prompting again. A pattern matches a command segment either exactly, or as a prefix when it ends with `*` (e.g. `cat *` matches `cat /tmp/x.html`, `git status` matches only that exact command).

Guidelines:
- Generalize to the user's likely intent, not just this one invocation — usually the program plus a trailing `*` (e.g. `cat *`, `ls *`, `npm run *`).
- Keep it tight enough to stay safe: capture the operation the user approved, not unrelated commands. Prefer one focused pattern; add more only if the command genuinely combines several programs.
- Never emit a bare `*` or an empty pattern.

Return the patterns as a JSON array of strings.
