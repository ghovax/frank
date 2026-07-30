Execute a bash command and return its output.

Synchronous by default: the command runs to completion and its real output is returned directly, so you always see the result of the action you took.

Set background=True only for genuinely long-running work you do NOT need the result of before your turn can continue — a build, a test suite, a dev server, a broad scan. A backgrounded command returns immediately with a task identifier; its result is auto-injected into the conversation when it finishes, and the harness re-engages you then. Do NOT background a command whose output you need next (and never background then re-run the same command — it is already running).

**Nothing you run may ask a question.** There is no terminal attached and nobody can type into it: a command that waits for input waits until its timeout, then auto-backgrounds still waiting, holding a process that will never finish. The user cannot rescue it and will not even see the prompt.

So say up front what an interactive command would have asked. Pass the flag that assumes the answer — `-y`, `--yes`, `--non-interactive`, `--no-input`, `--force-yes` where the tool offers it. Stop a pager before it starts: `git --no-pager log`, or pipe through `cat`. Never open an editor, a REPL, or a shell — no bare `python`, `node`, `psql`, `ssh host` without a command, no `git rebase -i` or `git add -i`, no `$EDITOR`. Feed input rather than waiting for it: `printf 'y\n' | …`, or `< /dev/null` to make the silence explicit. Never run anything that prompts for a password, `sudo` included — it cannot be answered here.

When a step genuinely needs a person — a credential, a decision only they can make, a confirmation the tool insists on — do not try to run it anyway. Ask with `ask_user`, or tell them what to run themselves.

Always provide a clear explanation and risk assessment for the command. Set read_only=True only for commands that provably just read state (cat, head, tail, ls, grep, find, etc.). Omitted, the command is treated as potentially mutating.

**Prefer specialized tools** for file discovery, content search, file reads, edits, writes, URL fetching, and downloads. Use bash for tests, builds, Git, process and package management, pipelines, and work without a dedicated tool.

**Work efficiently:** batch independent read-only commands, do not repeat a search whose answer is already available, and never run a broad recursive search over a user's home directory. Use ``background=True`` for managed long-running work instead of starting unmanaged ``&`` or ``nohup`` jobs.

Arguments:
  - command: The shell command to execute.
  - location: The project location to run the command on — its URI or name from the locations listed in your context. Defaults to the local filesystem; pass it only to target a different (remote) location.
  - read_only: Whether this command only reads state without modifying it. Defaults to False (treated as mutating) when omitted.
  - explanation: Explain why this command is needed for the task.
  - risk: One of "low", "medium", "high" — assess the potential damage. Low for read-only commands, medium for modifications, high for destructive operations.
  - background: Run the command in the background instead of waiting for it. Use for long-running work whose result is not needed immediately.
  - timeout: How many seconds to wait synchronously for the command before it auto-backgrounds (its result is then delivered when it finishes). Raise it for a command you want to wait longer for; it does not kill the command.
