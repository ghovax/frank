# The `daisy` command

`daisy` is the primary way to drive Daisy. It adds nothing the control plane does not have — it is the ergonomic face of it — so anything you can do here you can also do from the desktop app or from another session.

This command is for people. A session composes with its peers through [tools](tools.md#the-built-in-surface) over the same control plane, not by shelling out to this — a typed call can carry the caller's identity, which an argv string cannot, and a peer answers by messaging its parent rather than by being waited on.

That is enforced rather than merely advised. The daemon takes the identity of a caller on its unix socket from the kernel, and every command a session runs inherits that session's process session, so `daisy` run from inside one is attributed to it and scoped the way its own tools are: it can create, message, inspect and end sessions in its own subtree, and nothing else. A machine-wide `daisy ps` from inside a session comes back `403 forbidden` — `daisy tree` on itself is the question it is allowed to ask. From your own terminal, nothing is scoped.

The daemon starts itself on your first command, so there is no mandatory "start the service" step — `daisy serve` is for when you want it up on its own. And `daisy run` skips it entirely: one turn, in your terminal, no control plane at all.

## The shape of it

A **session** is one OS process running one agent. You create it empty, send it work, and read what it produced. Creating and working are separate steps on purpose: the same session takes a second task, can be attached to, and can be inspected in between.

```sh
id=$(daisy create --agent general-assistant --directory ~/code/project)
daisy send "$id" "what does this project do?" --wait
daisy ps
```

`create` prints the bare session id on stdout, which is what makes `id=$(daisy create …)` work in a shell script.

## Creating a session

```
daisy create [-a AGENT] [-C DIRECTORY] [-m MODE] [-p PROJECT] [-P PARENT] [-t TITLE]
```

| Flag | What it does |
|------|--------------|
| `-a`, `--agent` | **Required.** The agent profile to run. There is no default: which agent does the work is the one thing nothing can guess for you. |
| `-C`, `--directory` | The working directory. Project-local agents, skills and MCP servers are resolved from here. |
| `-m`, `--mode` | `default`, `auto`, or `read_only`. Fixed for the session's life. |
| `-p`, `--project` | The project this session belongs to. |
| `-P`, `--parent` | The session creating this one. The child is clamped to no looser a mode than its parent, and is reaped when the parent ends. Defaults to `$DAISY_SESSION_ID`, which every session exports — so this command run from inside a session creates a child of it rather than an orphan. |
| `-t`, `--title` | A label for the session list. Left unset, the session names itself after its first message. |

This is the **only** place a session's configuration is set. Its agent, its directory and its permission mode cannot be changed afterwards — that immutability is what makes a session's authority something you can reason about, rather than something a later call might widen.

A session created without a mode gets the configured default; a session created with one gets what it asked for, clamped against its parent. A mistyped `--agent` is refused at creation with the list of profiles that do exist, rather than minting a session that fails when first messaged.

## Sending work

```
daisy send <session> <message> [-w|--wait]
```

Pass `-` as the message to read it from stdin, which is how you send a file or a heredoc:

```sh
daisy send "$id" - <<'EOF'
Review the diff on this branch. Report anything that changes behaviour
without a test, and say what you would add.
EOF
```

`send` returns as soon as the message is accepted, printing the accepted turn. With `--wait` it follows the session until it goes idle and then prints the last turn — what the session produced, not its transcript.

A message that arrives while the session is mid-turn is **injected into that turn** at its next safe point rather than starting a second one. That is what lets you (or a peer) redirect a session that is already working instead of waiting for it to finish.

## Watching

```
daisy ps [-a|--all]          # what exists; --all includes sessions that have ended
daisy get <session>          # one session in detail
daisy tree <session>         # a session and everything it created
daisy attach <session>       # follow it live until you interrupt
daisy wait <session>         # block until idle, then print the result
daisy history <session> [-n N]
```

`ps` prints the session records as a JSON array. Three fields between them say what a session is, and they are separate because they answer genuinely different questions:

| Field | Meaning |
|-------|---------|
| `lifecycle` | Does it still exist? `live` or `ended`. **Durable** — it survives a daemon restart, because a session is a record and only its process was ever transient. `--all` includes the ended ones; `outcome` (`exited`/`failed`) and `exit_reason` say how and why. |
| `activity` | What it is doing *now*: `working` (a turn is in flight), `waiting` (parked on a decision only you can make), `idle` (has a process, doing nothing), `asleep` (**no process** — the next message forks one in about 60 ms), or `ended`. Derived on every read and never stored, because a stored "working" outlives the kill that made it false. |
| `awaiting_input` | Parked on a permission request or a question. It needs *you*. |

A session with no process is the normal resting state, not an error: an idle session is put to sleep immediately, and waking it is a fork. Reads never wake anything — `get`, `ps`, `tree`, `history` and `attach` are answered from the record and the turn store — so looking at a sleeping session leaves it asleep.

```sh
daisy ps | jq -r '.[] | select(.awaiting_input) | .id'
```

`attach` prints one JSON object per line as the session streams. Each carries a `kind`:

| `kind` | What it is |
|--------|------------|
| `snapshot` | The session's turns so far, sent first, so a watcher that attaches mid-turn is not guessing about what it missed. |
| `live` | One part of a turn as it is persisted — text, a tool call, a tool result, a permission request. |
| `turn` | A turn started or ended (`running`). This is what `wait` waits for: parts alone just stop arriving, which is indistinguishable from a model still thinking. |
| `done` | The session itself ended. Distinct from a turn ending — a session goes idle many times over its life. |

It ends when the session does; interrupt it with Ctrl-C to stop watching without affecting the session. Because each frame is a complete line, `jq` and friends consume it incrementally:

```sh
daisy attach "$id" | jq -r 'select(.kind == "live") | .message.text // empty'
```

## Answering a session

When a session needs permission it parks, `awaiting_input` goes true, and `attach` emits a frame carrying the request and its id. Answer it with that id:

```
daisy approve <session> <request> [-d|--deny]
```

There is no "always allow" and no bypass mode: every decision is allow-once or deny. That is a deliberate constraint — an approval you grant once cannot silently widen into a standing grant.

## Ending a session

```
daisy kill <session>
```

Ends the session and everything under it, children first, so a child never observes a dead parent.

"Everything under it" means the session's whole **process session**, not just the worker. Each session is spawned as a process-session leader, so every shell command it ran, and everything those commands started, carries its session id — a dev server a session left holding a port goes down with the session that started it. The process *group* is the wrong unit for this and used to be what was signalled: the `bash` tool deliberately puts each command in a group of its own so that cancelling one job reaps that job's subtree, which by construction put it outside a group-wide kill.

The one way to survive is for a process to call `setsid` and leave the session, which also takes it out of everything else the harness tracks. If you want something to outlive the session that started it, that is how — and it is then yours to stop.

## Agents on other hosts

```
daisy remote                        # the registered remote agents, with their live health
daisy remote <name> <message>       # hand one a message and print what it produced
```

A remote agent is not a session: it runs on someone else's machine, at their cost, with no shared history and no access to this filesystem. That is a different bargain from a peer session, so it is a different verb — you should never be unsure which side of the wire your work went to.

Registered in `~/.agents/remote-agents.json` by card URL, or from **Settings → Remote agents**. Their cards are resolved in the background, and a card that redirects to a private or loopback address is refused unless you opt in with `allow_private` — a remote agent's own card cannot be used to point Daisy at something inside your network.

## The interface in a browser

```
daisy web                               # http://127.0.0.1:8824
daisy web --port 9000
daisy web --no-daemon                   # serve without starting one
```

Serves the same interface the desktop app embeds, so a browser is a client like any other — useful on a headless machine, over an SSH tunnel, or anywhere you would rather not install an application.

It **proxies** the daemon rather than pointing the browser at it, and that is the whole design. Pointing would mean handing the daemon's capability token to a page, and would mean the page had to learn a port that is chosen fresh at every boot. Proxying attaches the token here, keeps it in this process, and puts everything on one origin — so there is no token in your browser's storage and no CORS to configure. Ordinary requests, the transcript's event stream, and the terminal's websocket all go the same way.

> [!WARNING]
> Whatever can reach this address can drive the daemon, because this server holds the token. It binds `127.0.0.1` for that reason. `--host` exists for tunnelling deliberately; if you use it, put authentication in front.

Needs the interface to have been built (`cd web && bun run build` in a checkout). The packaged build carries it.

## The desktop app

```
daisy app                               # start the daemon if needed, then launch the app
daisy app --no-daemon                   # just the window
```

The app is a **client**. It does not contain a daemon and does not start one — it finds one, reading the port and token `daisyd` publishes, and is powerless when there is none, exactly as it is when a remote host does not answer. So the convenience runs this way round: the command line, which owns the daemon, brings it up and then launches the window.

The app is addressed by bundle identifier rather than by name, so renaming or moving it does not break this. If it is not installed, the command says so rather than half-working. macOS only.

## Serving, and the daemon

```
daisy serve                             # start the control plane and detach
daisy serve --foreground                # run it here instead, for a log or a supervisor
daisy daemon status                     # what it is running, and where
daisy daemon stop                       # stop it, and its sessions' processes with it
daisy daemon restart                    # replace it; your sessions survive
daisy daemon endpoint                   # the loopback port and capability token
```

`serve` starts it; `daemon` inspects one that is already there. They are separate verbs because they are separate acts — starting the API is not a kind of introspection, and grouping them under one noun made `daemon start` read like a subcommand of looking at it. Any other command also starts a daemon if none is running, so `serve` is for wanting it up on its own.

`restart` **keeps your sessions**. Each one loses its process and comes back asleep, picking up where it left off on the next message; `sessions_slept` says how many that was. It exists because macOS caches the Accessibility trust check per process, so a daemon that was already running when you granted the permission never sees it — and the prototype it forks sessions from is a re-exec of it, so neither do they. The desktop app asks for the same thing over the control plane (`daemon.restart`), which is what makes the grant flow one click now that restarting the window no longer restarts the harness.

`stop` and `restart` signal the process group rather than calling the API, because a daemon wedged badly enough to need stopping may not be answering its own socket.

### Inspecting it

`daisy daemon status` reports whether it is up, how many sessions it knows about, and the prototype's health — including its native thread count and frozen-object count, which are the two invariants that fail silently.

`status` never starts anything — a status check that silently launched the service could never report the absence it was asked about. Pass `--start` if you want that.

`endpoint` prints a secret, which is why it is a verb you ask for rather than something `status` volunteers. It is what you need to point a desktop client at a daemon over SSH:

```sh
ssh workstation daisy daemon endpoint
```

## Configuration

```
daisy configure --all                    # every setting there is, with its default
daisy configure                          # only what you have changed
daisy configure agent.permission_mode    # read one
daisy configure agent.permission_mode read_only
daisy configure agent.permission_mode --unset
```

`--all` walks the **schema**, so it lists every setting that exists — not merely the ones somebody wrote down — as a JSON object of dotted path to `{about, default, current}`. That is usually what you want: reading the file can only show the part you already know about, and a setting left at its default was otherwise invisible.

With no argument it prints a JSON object of dotted path to value for what is actually set. With a setting it prints that setting's value bare — the file's value if it has one, otherwise what the code ships with — and puts the explanation and where the value came from on stderr, so a script reading stdout never has to strip it. Values are printed as they are stored, credentials included: this reads a file you own, and deciding on your behalf what you may see of your own configuration is not this command's business.

Values are interpreted the way the file holds them: `true`, `8` and `[]` land as a boolean, a number and a list rather than as the strings your shell handed over. `null` spells null; `none` does not, because it is a real value (`workspace.strategy: none` is the default) — use `--unset` to remove a setting.

A name the schema does not define, or a value it would reject, is refused with the reason, and the file is left as it was. The daemon reads this file at startup, so an invalid value would not fail the command that set it — it would fail every command after, including the one that would put it back. A name that is merely *unknown* is worse still: it would be written, listed back, and quietly do nothing.

Changes apply to what starts **next**. See the [Configuration guide](configuration.md) for what each setting means.

## Output, exit codes, and pipes

**Everything on stdout is plumbing.** A read prints the control plane's payload as JSON; a stream prints one JSON object per line; a verb whose answer *is* a single value prints that value bare, which is what makes `id=$(daisy create …)` work. There is no formatting layer, no colour, and no `--json` flag to remember — there is nothing else it could have been. Anything that wants a table pipes to `jq`, and anything that parses this never has to guess which mode it is in.

It is minified, and every JSON object is exactly one line — no indentation, and real UTF-8 rather than `\uXXXX` escapes. Agents drive these verbs constantly and pay for indentation by the token; pipe through `jq .` when you want it laid out for a person.

Diagnostics go to stderr and outcomes go to the exit code, so neither can contaminate the data. `daisy configure some.setting` on a stderr-suppressed pipeline prints the value or nothing at all; it never prints an apology you would then have to parse around.

| Exit code | Meaning |
|-----------|---------|
| `0` | Success. |
| `1` | The call failed — no such session, the daemon is unreachable, an unknown setting. |
| `2` | The arguments were wrong (argparse). |
| `130` | Interrupted with Ctrl-C. |
| `141` | A pipe closed under it (`daisy ps \| head`). |

## What each verb calls

The CLI is the ergonomic face of the control plane, and it is allowed to be idiomatic where the idiom is strong — `ps` and `kill` are what a shell user reaches for. Everywhere the names differ, this is why:

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
| `serve` | starts `daisyd` — no method, it *is* the thing being started |
| `run` | none: it drives `daisy.Session` in this process, with no daemon at all |
| `auth` | none: it writes the credential file the harness reads |

## One turn, without a daemon

```sh
daisy run "what does this project do?"
daisy run -C ~/code/project --agent reviewer "what changed and is it safe?"
echo "summarise this" | daisy run -
daisy run --allow "run the tests and tell me what failed"
```

`run` is the whole harness with none of the control plane. It drives `daisy.Session` in this process — the same library surface an embedder uses — prints the agent's prose as it arrives, and exits. No session record, no address, no crash isolation: reach for `create` and `send` when you want any of those. This is for a question with an answer.

`--allow` answers every permission gate with yes. Without it, a turn that needs a decision stops and says so, because nobody is watching. `--json` prints the turn events instead of the prose, which is the same vocabulary `attach` streams.

## Signing in

```sh
daisy auth login                        # open the browser, sign in to ChatGPT
daisy auth status                       # who is signed in, if anyone
daisy auth logout
```

Only ChatGPT works this way — every other provider takes an API key through `daisy configure`. It is a verb rather than a setting because the credential is not something you can type: it is an OAuth exchange that lands on a loopback callback. Before this it existed only inside the browser interface, which meant a headless install could not reach the one provider that needs no key.

## Talking to a session directly

`daisy` reaches the daemon over its unix socket and posts every command to it, `send` included; the daemon relays to the owning session. You can also address a session yourself, which is what makes the relay a hop rather than a wall: each one serves [A2A](https://github.com/google/A2A) on `$XDG_RUNTIME_DIR/daisy/sessions/<id>.sock`, and `create` returns the capability token that authorises driving it. Discovery is open — a session's card at `/.well-known/agent-card.json` says what it is — but every other call must present the token.

That is the whole composition model. A peer is not a special kind of thing; it is a session, addressed the way you address any session.
