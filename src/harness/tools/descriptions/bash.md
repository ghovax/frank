**Execute a bash command** and return its output.

**Synchronous by default.** The command runs to completion and its real output is returned to you directly, so you always see the result of the action you took. This is what you want for almost everything — including quick git/`gh`, network, and package commands that take a few seconds.

**`background=true` is opt-in, for long-running work you do not need the result of right now** — a build, a full test suite, a dev server, a broad scan. A backgrounded command returns immediately with a `task_identifier`; its result is **auto-injected** into the conversation when it finishes, and the harness starts a fresh turn to re-engage you the moment it completes (even minutes later), so a long job is never lost and never holds a turn open. If the rest of your work depends on the result, finish your turn after backgrounding and you will be woken.

- **Never background a command whose output you need next**, then wait or poll for it — run it synchronously and read the result.
- **Never background a command and then re-run the same command** because it "looked unfinished." The backgrounded one is already running; re-issuing a mutating command (a merge, a push, a deploy) double-executes it. If you backgrounded it, end your turn and wait for the injected result.

**Prefer the specialized tools over shell** for the operations they cover — they are faster, cheaper, and give the model better-shaped results:
- *File search:* use **find_files** (not `find` or `ls`)
- *Content search:* use **search_content** (not `grep` or `rg`)
- *Read files:* use **read_file** (not `cat`, `head`, `tail`, `sed -n`)
- *Edit files:* use **edit_file** (not `sed`, `awk`)
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
- Heavy commands, long tests/builds, servers, and broad scans are the case for `background=true`; do not start unmanaged detached jobs by hand (`&`, `nohup`).

Always provide a concise **justification** that states *why* this command advances the task — let it read as one smooth, open-ended sentence, never a `label: detail` heading.
