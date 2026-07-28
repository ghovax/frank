# The `frank` command

`frank` is the primary way to drive Frank. It adds nothing the control plane does not have; it is the ergonomic face of it. Anything you can do here, you can also do from the desktop app, or from another session.

This command is for people. A session composes with its peers through [tools](tools.md#the-built-in-surface), over the same control plane. It does not shell out to this command. A typed call carries the caller's identity, which an argv string cannot. A peer also answers by messaging its parent; nothing waits on it.

That is enforced rather than merely advised. The daemon takes a caller's identity on its unix socket from the kernel. Every command a session runs inherits that session's process session.

`frank` run from inside a session is therefore attributed to it, and scoped the way its own tools are. It can create, message, inspect and end sessions in its own subtree, and nothing else. A machine-wide `frank ps` from inside a session comes back `403 forbidden` — `frank tree` on itself is the question it is allowed to ask. From your own terminal, nothing is scoped.

The daemon starts itself on your first command. There is no mandatory "start the service" step. `frank serve` is for when you want it up on its own. And `frank run` skips it entirely: one turn, in your terminal, no control plane at all.

## The shape of it

A **session** is one OS process running one agent. You create it empty, send it work, and read what it produced. Creating and working are separate steps on purpose: the same session takes a second task, can be attached to, and can be inspected in between.

```shell
id=$(frank create --agent general-assistant --directory ~/code/project)
frank send "$id" "what does this project do?" --wait
frank ps
```

`create` prints the bare session id on stdout, which is what makes `id=$(frank create …)` work in a shell script.

## Creating a session

```text
frank create [-a AGENT] [-C DIRECTORY] [-m MODE] [-p PROJECT] [-P PARENT] [-t TITLE]
```

| Flag | What it does |
|------|--------------|
| `-a`, `--agent` | **Required.** The agent profile to run. There is no default: which agent does the work is the one thing nothing can guess for you. |
| `-C`, `--directory` | The working directory. Project-local agents, skills and MCP servers are resolved from here. |
| `-m`, `--mode` | `default`, `auto`, or `read_only`. Fixed for the session's life. |
| `-p`, `--project` | The project this session belongs to. |
| `-P`, `--parent` | The session creating this one. The child is clamped to no looser a mode than its parent, and is reaped when the parent ends. Defaults to `$FRANK_SESSION_ID`, which every session exports — so this command run from inside a session creates a child of it rather than an orphan. |
| `-t`, `--title` | A label for the session list. Left unset, the session names itself after its first message. |

This is the **only** place a session's configuration is set. Nothing changes its agent, its directory, or its permission mode afterwards. That immutability makes a session's authority something you can reason about. A later call cannot widen it.

A session created without a mode gets the configured default; a session created with one gets what it asked for, clamped against its parent. A mistyped `--agent` is refused at creation with the list of profiles that do exist, rather than minting a session that fails when first messaged.

## Sending work

```text
frank send <session> <message> [-w|--wait]
```

Pass `-` as the message to read it from stdin, which is how you send a file or a heredoc:

```shell
frank send "$id" - <<'EOF'
Review the diff on this branch. Report anything that changes behaviour
without a test, and say what you would add.
EOF
```

`send` returns as soon as the message is accepted, printing the accepted turn. With `--wait` it follows the session until it goes idle and then prints the last turn — what the session produced, not its transcript.

A message that arrives while the session is mid-turn is **injected into that turn** at its next safe point rather than starting a second one. That is what lets you (or a peer) redirect a session that is already working instead of waiting for it to finish.

## Watching

| Command | What it does |
|---|---|
| `frank ps [-a\|--all]` | Shows what exists. `--all` includes sessions that ended |
| `frank get <session>` | One session in detail |
| `frank tree <session>` | A session and everything it created |
| `frank attach <session>` | Follow it live until you interrupt |
| `frank wait <session>` | Block until idle, then print the result |
| `frank history <session> [-n N]` | Prints the last N turns |

`ps` prints the session records as a JSON array. Three fields between them say what a session is, and they are separate because they answer genuinely different questions:

| Field | Meaning |
|-------|---------|
| `lifecycle` | Does it still exist? `live` or `ended`. **Durable** — it survives a daemon restart, because a session is a record and only its process was ever transient. `--all` includes the ended ones; `outcome` (`exited`/`failed`) and `exit_reason` say how and why. |
| `activity` | What it is doing *now*: `working` (a turn is in flight), `waiting` (parked on a decision only you can make), `idle` (has a process, doing nothing), `asleep` (**no process** — the next message forks one in about 60 ms), or `ended`. Derived on every read and never stored, because a stored "working" outlives the kill that made it false. |
| `awaiting_input` | Parked on a permission request or a question. It needs *you*. |

A session with no process is the normal resting state, not an error. An idle session sleeps immediately, and to wake it is a fork. Reads never wake anything. The record and the turn store answer `get`, `ps`, `tree`, `history` and `attach`. To look at a sleeping session therefore leaves it asleep.

```shell
frank ps | jq -r '.[] | select(.awaiting_input) | .id'
```

`attach` prints one JSON object per line as the session streams. Each carries a `kind`:

| `kind` | What it is |
|--------|------------|
| `snapshot` | The session's turns so far, sent first, so a watcher that attaches mid-turn is not guessing about what it missed. |
| `live` | One part of a turn as it is persisted — text, a tool call, a tool result, a permission request. |
| `turn` | A turn started or ended (`running`). This is what `wait` waits for: parts alone just stop arriving, which is indistinguishable from a model still thinking. |
| `done` | The session itself ended. Distinct from a turn ending — a session goes idle many times over its life. |

It ends when the session does; interrupt it with Ctrl-C to stop watching without affecting the session. Because each frame is a complete line, `jq` and friends consume it incrementally:

```shell
frank attach "$id" | jq -r 'select(.kind == "live") | .part.text // empty'
```

## Answering a session

When a session needs permission it parks, `awaiting_input` goes true, and `attach` emits a frame carrying the request and its id. Answer it with that id:

```text
frank approve <session> <request> [-d|--deny]
```

There is no "always allow" and no bypass mode: every decision is allow-once or deny. That is a deliberate constraint — an approval you grant once cannot silently widen into a standing grant.

## Ending a session

```text
frank kill <session>
```

Ends the session and everything under it, children first, so a child never observes a dead parent.

"Everything under it" means the session's whole **process session**, not just the worker. Each session spawns as a process-session leader. Every shell command it ran, and everything those commands started, therefore carries its session id.

A dev server that a session left holding a port goes down with the session that started it. The process *group* is the wrong unit for this. The `bash` tool deliberately puts each command in a group of its own, so that a cancelled job reaps that job's subtree. That by construction puts the command outside a group-wide kill.

A process survives one way only: it calls `setsid` and leaves the session. That also takes it out of everything else the harness tracks. If you want something to outlive the session that started it, that is how — and it is then yours to stop.

## Agents on other hosts

| Command | What it does |
|---|---|
| `frank remote` | The registered remote agents, with their live health |
| `frank remote <name> <message>` | Hand one a message and print what it produced |

A remote agent is not a session. It runs on someone else's machine, at their cost. It has no shared history and no access to this filesystem. That is a different bargain from a peer session, so it is a different verb. You must never be unsure which side of the wire your work went to.

Registered in `~/.agents/remote-agents.json` by card URL, or from **Settings → Remote agents**. Frank resolves their cards in the background. It refuses a card that redirects to a private or loopback address, unless you opt in with `allow_private`. A remote agent's own card therefore cannot point Frank at something inside your network.

## The interface in a browser

| Command | What it does |
|---|---|
| `frank web` | Http://127.0.0.1:8824 |
| `frank web --port 9000` |  |
| `frank web --no-daemon` | Serve without starting one |

This serves the same interface the desktop app embeds, so a browser is a client like any other. It is useful on a headless machine, over an SSH tunnel, or anywhere you would rather not install an application.

It **proxies** the daemon rather than pointing the browser at it, and that is the whole design. To point at the daemon would hand its capability token to a page. The page would also have to learn a port that is chosen fresh at every boot. A proxy attaches the token here, keeps it in this process, and puts everything on one origin. There is therefore no token in your browser's storage, and no CORS to configure. Ordinary requests, the transcript's event stream, and the terminal's websocket all go the same way.

> [!WARNING]
> Whatever can reach this address can drive the daemon, because this server holds the token. It binds `127.0.0.1` for that reason. `--host` exists for tunnelling deliberately; if you use it, put authentication in front.

Needs the interface to have been built (`cd web && bun run build` in a checkout). The packaged build carries it.

## The desktop app

| Command | What it does |
|---|---|
| `frank app` | Start the daemon if needed, then launch the app |
| `frank app --no-daemon` | Just the window |

The app is a **client**. It contains no daemon, and it starts none. It finds one by reading the port and token that `frankd` publishes. When there is none it is powerless, exactly as it is when a remote host does not answer. So the convenience runs this way round: the command line, which owns the daemon, brings it up and then launches the window.

The app is addressed by bundle identifier rather than by name, so renaming or moving it does not break this. If it is not installed, the command says so rather than half-working. macOS only.

## Serving, and the daemon

| Command | What it does |
|---|---|
| `frank serve` | Start the control plane and detach |
| `frank serve --foreground` | Run it here instead, for a log or a supervisor |
| `frank daemon status` | What it is running, and where |
| `frank daemon stop` | Stop it, and its sessions' processes with it |
| `frank daemon restart` | Replace it; your sessions survive |
| `frank daemon endpoint` | The loopback port and capability token |

`serve` starts it; `daemon` inspects one that is already there. They are separate verbs because they are separate acts. To start the API is not a kind of introspection. Grouped under one noun, `daemon start` read like a subcommand of looking at it. Any other command also starts a daemon if none is running, so `serve` is for wanting it up on its own.

`restart` **keeps your sessions**. Each one loses its process and comes back asleep, picking up where it left off on the next message; `sessions_slept` says how many that was. It exists because macOS caches the Accessibility trust check per process.

A daemon that was already running when you granted the permission therefore never sees it. The prototype it forks sessions from is a re-exec of it, so neither do the sessions. The desktop app asks for the same thing over the control plane, with `daemon.restart`. That makes the grant flow one click, because a restart of the window does not restart the harness.

`stop` and `restart` signal the process group; they do not call the API. A daemon wedged badly enough to need stopping may not answer its own socket.

### Inspecting it

`frank daemon status` reports whether the daemon is up, how many sessions it knows about, and the prototype's health:

```console
$ frank daemon status
{"ok":true,"sessions":{"live":64,"total":73},"prototype":{"alive":true,"pid":30054,
 "threads":1,"frozen_objects":18422,"sessions":0},"port":56826,
 "image":{"executable":"…/frank","frozen":true}}
```

`threads` and `frozen_objects` are the two invariants that fail silently. The prototype must be
single-threaded to fork at all, and its heap must be frozen or the saving disappears without
anything breaking.

`status` never starts anything — a status check that silently launched the service could never report the absence it was asked about. Pass `--start` if you want that.

`endpoint` prints a secret, which is why it is a verb you ask for rather than something `status` volunteers. It is what you need to point a desktop client at a daemon over SSH:

```shell
ssh workstation frank daemon endpoint
```

## Configuration

| Command | What it does |
|---|---|
| `frank configure --all` | Every setting there is, with its default |
| `frank configure` | Only what you have changed |
| `frank configure agent.permission_mode` | Read one |
| `frank configure agent.permission_mode read_only` | Set one |
| `frank configure agent.permission_mode --unset` | Remove one, back to its default |

`--all` walks the **schema**. It therefore lists every setting that exists, not only the ones somebody wrote down. The output is a JSON object of dotted path to `{about, default, current}`. That is usually what you want. To read the file shows only the part you already know about. A setting left at its default was otherwise invisible.

With no argument it prints a JSON object of dotted path to value for what is actually set. With a setting, it prints that setting's value bare: the file's value if there is one, otherwise what the code ships with. The explanation, and where the value came from, go to stderr.

A script that reads stdout therefore never has to strip them. It prints values as they are stored, credentials included. This reads a file you own. To decide on your behalf what you may see of your own configuration is not this command's business.

It interprets values the way the file holds them. `true`, `8` and `[]` land as a boolean, a number and a list. They do not land as the strings your shell handed over. `null` spells null; `none` does not, because it is a real value (`workspace.strategy: none` is the default) — use `--unset` to remove a setting.

A name the schema does not define, or a value it would reject, is refused with the reason. The file is left as it was. The daemon reads this file at startup. An invalid value would therefore not fail the command that set it. It would fail every command after, including the one that would put it back. A name that is merely *unknown* is worse still: it would be written, listed back, and quietly do nothing.

Changes apply to what starts **next**. See the [Configuration guide](configuration.md) for what each setting means.

## Output, exit codes, and pipes

**Everything on stdout is plumbing.** A read prints the control plane's payload as JSON. A stream prints one JSON object per line. A verb whose answer *is* a single value prints that value bare, which is what makes `id=$(frank create …)` work. There is no formatting layer, no colour, and no `--json` flag to remember — there is nothing else it could have been. Anything that wants a table pipes to `jq`, and anything that parses this never has to guess which mode it is in.

It is minified, and every JSON object is exactly one line — no indentation, and real UTF-8 rather than `\uXXXX` escapes. Agents drive these verbs constantly and pay for indentation by the token; pipe through `jq .` when you want it laid out for a person.

Diagnostics go to stderr and outcomes go to the exit code, so neither can contaminate the data. `frank configure some.setting` on a stderr-suppressed pipeline prints the value or nothing at all; it never prints an apology you would then have to parse around.

| Exit code | Meaning |
|-----------|---------|
| `0` | Success. |
| `1` | The call failed — no such session, the daemon is unreachable, an unknown setting. |
| `2` | The arguments were wrong (argparse). |
| `130` | Interrupted with Ctrl-C. |
| `141` | A pipe closed under it (`frank ps \| head`). |

## What each verb calls

The CLI is the ergonomic face of the control plane. It may be idiomatic where the idiom is strong. `ps` and `kill` are what a shell user reaches for. Everywhere the names differ, this is why:

| Verb | Control-plane method |
|------|----------------------|
| `create` | `session.create` |
| `send` | `session.send` |
| `get` | `session.get` |
| `ps` | `session.list` |
| `tree` | `session.tree` |
| `history` | `session.history` |
| `attach` / `wait` | `GET /sessions/{id}/attach` |
| `approve` | `session.respond` |
| `kill` | `session.end` |
| `remote` | `remote.list` / `remote.send` |
| `daemon status` | `daemon.status` |
| `serve` | Starts `frankd` — no method, it *is* the thing being started |
| `run` | None: it drives `frank.Session` in this process, with no daemon at all |
| `auth` | None: it writes the credential file the harness reads |

## One turn, without a daemon

```shell
frank run "what does this project do?"
frank run -C ~/code/project --agent reviewer "what changed and is it safe?"
echo "summarise this" | frank run -
frank run --allow "run the tests and tell me what failed"
```

`run` is the whole harness with none of the control plane. It drives `frank.Session` in this process — the same library surface an embedder uses — prints the agent's prose as it arrives, and exits. No session record, no address, no crash isolation: reach for `create` and `send` when you want any of those. This is for a question with an answer.

`--allow` answers every permission gate with yes. Without it, a turn that needs a decision stops and says so, because nobody is watching. `--json` prints the turn events instead of the prose, which is the same vocabulary `attach` streams.

## Signing in

| Command | What it does |
|---|---|
| `frank auth login` | Open the browser, sign in to ChatGPT |
| `frank auth status` | Who is signed in, if anyone |
| `frank auth logout` |  |

Only ChatGPT works this way — every other provider takes an API key through `frank configure`. It is a verb, not a setting, because you cannot type the credential. It is an OAuth exchange that lands on a loopback callback. It is a command so that a headless install can reach the one provider that needs no key.

## Talking to a session directly

`frank` reaches the daemon over its unix socket and posts every command to it, `send` included; the daemon relays to the owning session. You can also address a session yourself, which makes the relay a hop rather than a wall. Each session serves [A2A](https://github.com/google/A2A) on `$XDG_RUNTIME_DIR/frank/sessions/<id>.sock`, and `create` returns the capability token that authorises driving it. Discovery is open — a session's card at `/.well-known/agent-card.json` says what it is — but every other call must present the token.

That is the whole composition model. A peer is not a special kind of thing; it is a session, addressed the way you address any session.
