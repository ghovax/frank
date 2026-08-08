Run a bash command and return its output. This is how you read the filesystem, search it, and change it.

The command runs to completion by default, and you get its real output, so you always see the result of what you did.

Set `background=True` only for long work whose result you do not need before your turn continues: a build, a test suite, a development server, a broad scan. A backgrounded command returns at once with a task identifier, its result reaches the conversation when it finishes, and the harness re-engages you then. Do not background a command whose output you need next, and never background a command and then run it again — it already runs.

## Reading and changing files

Every file operation composes from the tools already on this machine, and the shell is what composes them. What follows are the rules that make a composition sound, not a set of recipes: the right command for a given job follows from them.

**Read only what you will use.** A whole file is the widest possible read, so reach for it only when you genuinely need the whole thing. Prefer the tool that answers the question you actually have: a matcher when you want the lines that mention something, a range extractor when you know where to look, a counter when you want a size, a structural query when you want a definition rather than a string. Line numbers are worth requesting whenever you intend to come back to a position.

**Match the tool to the shape of the change.** A change to one known place wants an exact, anchored replacement. A change repeated across many places wants a stream editor or a scripted pass. A change whose correctness depends on structure — balanced brackets, indentation that carries meaning, a syntax tree — wants a real parser rather than a pattern. Choosing a pattern for a structural job is the most common way to corrupt a file.

**A replacement must identify exactly one thing, or say that it means all of them.** Anchor on enough surrounding context to be unique. Where you intend to change every occurrence, say so explicitly rather than relying on a pattern that happens to match once today.

**Prefer a program to a pipeline once the logic branches.** Sequential filters are right for a straight line of transformations. The moment the work needs a condition, a loop over parsed structures, or a second pass that depends on the first, write it as a script and run that: it is easier to get right, easier to read back, and it fails in one place instead of silently in the middle.

**Verify by reading back what you wrote**, not by trusting that the command reported success. A tool that exits zero has told you it ran, not that the file says what you meant. Read the changed region, or run the check that would catch a mistake — a parser, a type checker, a test — before you move on.

**Write in a way that survives its own content.** Text you are writing may contain quotes, backslashes, dollar signs or the delimiter you chose. Use a quoting form that makes the shell leave the body alone, and pick a delimiter that cannot occur in what you are writing.

**Keep an in-place edit atomic where the file matters.** A stream editor that writes over its own input can truncate the file if the command fails midway. Use the tool's own in-place mode where it has a real one, or write beside the file and move it into place.

### What not to do

- **Do not read a file merely to confirm it exists**, or to see something the last command already printed. The output you already hold is a read you do not have to repeat.
- **Do not read a whole file to change one line**, and do not read it again after an edit merely to reassure yourself when the check you actually need is a parse or a test.
- **Do not use a line-oriented tool on a format where a line is not a unit** — a nested document, a serialized structure, a language whose statements span lines. Use a parser for that format.
- **Do not anchor a replacement on something generic** such as a lone brace, a common keyword or an indent. It will match somewhere you did not look.
- **Do not chain a destructive step behind an unchecked one.** Separate the step that computes from the step that overwrites, and look at what the first produced.
- **Do not paginate.** Nothing here reads a pager, so send output through a filter or a cap rather than through something that waits for a keypress.
- **Never point a recursive search at a dense directory** such as a home directory or a whole volume. Scope it to the project, to a known subdirectory, or to an exact pattern.

## Bringing in what you need

**Any library is available to you, so use one rather than reimplementing it.** Where the work suits Python, run it through `uv`, which resolves and installs dependencies for a script without touching anything outside this session: declare what the script imports and let `uv` fetch it. The same freedom applies to whatever this session's package manager installs.

Do not talk yourself into a worse implementation to avoid a dependency, and do not hand-roll parsing, formatting, diffing or serialization that a well-known library does correctly. Reach for the library first, and let the composition follow from what it gives you.

## Nothing you run may ask a question

No terminal is attached, and nobody can type into it. A command that waits for input waits until its timeout, then moves to the background still waiting, holding a process that will never finish. The user cannot rescue it and never even sees the prompt.

So say up front what an interactive command would have asked.

- Pass the flag that answers the question: `-y`, `--yes`, `--non-interactive`, `--no-input`, or `--force-yes` where the tool has one.
- Stop a pager before it starts, with the tool's own flag or by piping the output onward.
- Never open an editor, a REPL or a shell: no bare `python`, `node` or `psql`, no `ssh host` without a command, no interactive `git`, and never `$EDITOR`.
- Feed the input instead of waiting for it, or redirect from `/dev/null` to make the silence explicit.
- Never run anything that asks for a password, and that includes `sudo`. Nobody can answer it here.

Where a step truly needs a person — a credential, a decision only they can make, a confirmation the tool insists on — do not try to run it anyway. Ask with `ask_user`, or tell the user what to run themselves.

## Saying what a command reaches

Always use `access_request` to state whether the command changes anything, and add a path or the network only when the command needs reach beyond what your confinement already allows. Your context lists that confinement, so read it first: a write outside it fails with a permission error that names no path.

Inside that confinement you are not interrupted, so there is no reason to narrow what you run in the hope of being asked less. Ask for the narrowest reach that does the work, and only when the work is genuinely outside the box, since a request wider than its explanation justifies is refused on its own.

**Work efficiently.** Batch independent read-only commands into one call, and chain deterministic steps with `&&` and pipes. Do not repeat a search whose answer you already hold. Use `background=True` for long work the harness manages, instead of starting an unmanaged `&` or `nohup` job.

Arguments:
  - command: The shell command to run.
  - location: Which workspace location runs the command — its URI or its name, from the locations listed in your context. Defaults to the local filesystem. Pass it only to reach a different, remote location.
  - access_request: What this command says about changing anything, and what it needs beyond the session's confinement. Always set `mutates`; add `writes`, `reads`, or `network` only for reach the confinement does not already provide.
  - explanation: Why the task needs this command.
  - background: Run the command in the background instead of waiting for it. Use this for long work whose result you do not need now.
  - timeout: How many seconds to wait for the command before it moves to the background, where its result reaches you when it finishes. Raise it for a command you want to wait longer for. It does not kill the command.
