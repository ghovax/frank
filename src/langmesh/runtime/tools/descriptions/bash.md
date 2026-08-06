Run a bash command and return its output.

The command runs to completion by default, and you get its real output. You always see the result of what you did.

Set `background=True` only for long work whose result you do not need before your turn continues: a build, a test suite, a development server, a broad scan. A backgrounded command returns at once with a task identifier. Its result reaches the conversation when it finishes, and the harness re-engages you then. Do not background a command whose output you need next, and never background a command and then run it again — it already runs.

**Nothing you run may ask a question.** No terminal is attached, and nobody can type into it. A command that waits for input waits until its timeout. It then moves to the background, still waiting, and holds a process that will never finish. The user cannot rescue it and never even sees the prompt.

So say up front what an interactive command would have asked.

- Pass the flag that answers the question: `-y`, `--yes`, `--non-interactive`, `--no-input`, or `--force-yes` where the tool has one.
- Stop a pager before it starts. Use `git --no-pager log`, or pipe the output through `cat`.
- Never open an editor, a REPL or a shell. Do not run a bare `python`, `node` or `psql`. Do not run `ssh host` without a command. Do not run `git rebase -i` or `git add -i`. Do not invoke `$EDITOR`.
- Feed the input instead of waiting for it: `printf 'y\n' | …`, or `< /dev/null` to make the silence explicit.
- Never run anything that asks for a password, and that includes `sudo`. Nobody can answer it here.

Where a step truly needs a person — a credential, a decision only they can make, a confirmation the tool insists on — do not try to run it anyway. Ask with `ask_user`, or tell the user what to run themselves.

**Say what the command reaches.** Give a clear `explanation`. Use `access_request` to state whether the command changes anything, and to ask for a path or for the network beyond what your confinement already allows. Your context lists that confinement, so read it first: a write outside it fails with a permission error that names no path.

Inside that confinement you are not interrupted, so there is no reason to narrow what you run in the hope of being asked less. Ask for the narrowest reach that does the work, and only when the work is genuinely outside the box: a request wider than the explanation justifies is refused on its own.

**Prefer a specialized tool** for finding files, searching content, reading files, editing, writing, fetching a URL, and downloading. Use `bash` for tests, builds, git, processes, package management, pipelines, and work that has no dedicated tool.

**Work efficiently.** Batch independent read-only commands. Do not repeat a search whose answer you already hold. Never run a broad recursive search over the user's home directory. Use `background=True` for long work that the harness manages, instead of starting an unmanaged `&` or `nohup` job.

Arguments:
  - command: The shell command to run.
  - location: Which workspace location runs the command — its URI or its name, from the locations listed in your context. Defaults to the local filesystem. Pass it only to reach a different, remote location.
  - access_request: What this call needs beyond what the session already holds. Omit it where the command works inside the confinement listed in your context, which is the usual case. When present it must set `mutates`. Use `writes` and `reads` for paths outside the confinement, and `network` only where the confinement denies the network.
  - explanation: Why the task needs this command.
  - background: Run the command in the background instead of waiting for it. Use this for long work whose result you do not need now.
  - timeout: How many seconds to wait for the command before it moves to the background, where its result reaches you when it finishes. Raise it for a command you want to wait longer for. It does not kill the command.
