**Execute a bash command** and return its output.

Fast commands (under ~2s) return output directly. Slow commands return immediately with a task identifier and output file path — the result is **auto-injected** into the conversation when it finishes.

**Prefer the specialized tools over shell** for the operations they cover — they are faster, cheaper, and give the model better-shaped results:
- *File search:* use **find_files** (not `find` or `ls`)
- *Content search:* use **search_content** (not `grep` or `rg`)
- *Read files:* use **read_lines** (not `cat`, `head`, `tail`, `sed -n`)
- *Edit files:* use **apply_patch** (not `sed`, `awk`)
- *Write files:* use **write_file** (not `echo >`, `cat <<EOF`)
- *Fetch a URL:* use **fetch_url** (not `curl`/`wget` for reading)

Reach for `bash` for everything else: tests, builds, git, process and package management, pipelines, and anything without a dedicated tool.

**Safety contract** — treat the `read_only` flag as part of the contract, not decoration:
- `read_only=true` for commands that only **inspect state** (`pwd`, `ls`, `rg`, `cat`, `sed -n`, `git diff`, `git status`).
- `read_only=false` for anything that **modifies** files, processes, caches, databases, or external state. Set `risk` accordingly:
  - `low` — targeted project-local edit or safe generated output.
  - `medium` — broad rewrite, dependency change, process management, database write, nontrivial side effects.
  - `high` — destructive, privileged, irreversible, or system-level change.

**Efficiency** (the user sees your activity live):
- **Batch independent read-only commands** (6–12 at once) when they answer the same question.
- Do **not** repeat a search you already have the answer to.
- Check your session context before searching — a prior result may already contain what you need.
- Never run broad recursive searches or directory walks over the real home directory (`~` or `/Users/<name>`). Narrow to the project, a known subdirectory, or exact paths.
- Heavy commands, long tests/builds, servers, and broad scans are expected to run as harness background tasks through this tool; do not start unmanaged detached jobs.

Always provide a concise **justification** that states *why* this command advances the task.
