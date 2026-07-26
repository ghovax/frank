# The `xeac` command

`xeac` is the primary way to drive XEAC. It adds nothing the control plane does not have — it is the ergonomic face of it — so anything you can do here you can also do from the desktop app or from another session.

This command is for people. A session composes with its peers through [tools](tools.md#the-built-in-surface) over the same control plane, not by shelling out to this — a typed call can carry the caller's identity, which an argv string cannot, and a peer answers by messaging its parent rather than by being waited on.

That is enforced rather than merely advised. The daemon takes the identity of a caller on its unix socket from the kernel, and every command a session runs inherits that session's process session, so `xeac` run from inside one is attributed to it and scoped the way its own tools are: it can create, message, inspect and end sessions in its own subtree, and nothing else. A machine-wide `xeac ps` from inside a session comes back `403 forbidden` — `xeac tree` on itself is the question it is allowed to ask. From your own terminal, nothing is scoped.

The daemon starts itself on your first command. There is no separate "start the service" step.

## The shape of it

A **session** is one OS process running one agent. You create it empty, send it work, and read what it produced. Creating and working are separate steps on purpose: the same session takes a second task, can be attached to, and can be inspected in between.

```sh
id=$(xeac create --agent general-assistant --directory ~/code/project)
xeac send "$id" "what does this project do?" --wait
xeac ps
```

`create` prints the bare session id on stdout, which is what makes `id=$(xeac create …)` work in a shell script.

## Creating a session

```
xeac create [-a AGENT] [-C DIRECTORY] [-m MODE] [-p PROJECT] [-P PARENT] [-t TITLE]
```

| Flag | What it does |
|------|--------------|
| `-a`, `--agent` | **Required.** The agent profile to run. There is no default: which agent does the work is the one thing nothing can guess for you. |
| `-C`, `--directory` | The working directory. Project-local agents, skills and MCP servers are resolved from here. |
| `-m`, `--mode` | `default`, `auto`, or `read_only`. Fixed for the session's life. |
| `-p`, `--project` | The project this session belongs to. |
| `-P`, `--parent` | The session creating this one. The child is clamped to no looser a mode than its parent, and is reaped when the parent ends. Defaults to `$XEAC_SESSION_ID`, which every session exports — so this command run from inside a session creates a child of it rather than an orphan. |
| `-t`, `--title` | A label for the session list. Left unset, the session names itself after its first message. |

This is the **only** place a session's configuration is set. Its agent, its directory and its permission mode cannot be changed afterwards — that immutability is what makes a session's authority something you can reason about, rather than something a later call might widen.

A session created without a mode gets the configured default; a session created with one gets what it asked for, clamped against its parent. A mistyped `--agent` is refused at creation with the list of profiles that do exist, rather than minting a session that fails when first messaged.

## Sending work

```
xeac send <session> <message> [-w|--wait]
```

Pass `-` as the message to read it from stdin, which is how you send a file or a heredoc:

```sh
xeac send "$id" - <<'EOF'
Review the diff on this branch. Report anything that changes behaviour
without a test, and say what you would add.
EOF
```

`send` returns as soon as the message is accepted, printing the accepted turn. With `--wait` it follows the session until it goes idle and then prints the last turn — what the session produced, not its transcript.

A message that arrives while the session is mid-turn is **injected into that turn** at its next safe point rather than starting a second one. That is what lets you (or a peer) redirect a session that is already working instead of waiting for it to finish.

## Watching

```
xeac ps [-a|--all]          # what exists; --all includes sessions that have ended
xeac get <session>          # one session in detail
xeac tree <session>         # a session and everything it created
xeac attach <session>       # follow it live until you interrupt
xeac wait <session>         # block until idle, then print the result
xeac history <session> [-n N]
```

`ps` prints the session records as a JSON array. Three fields between them say what a session is doing, and they are separate because they answer different questions:

| Field | Meaning |
|-------|---------|
| `status` | The *process*: `starting` while its socket is not yet accepting connections, `running` once it is, `exited` or `failed` when it is over. `--all` includes the terminal ones; `exit_reason` says why. |
| `busy` | Whether a turn is actually in flight. A session's process outlives its turns, so `running` alone cannot tell a working session from an idle one. |
| `awaiting_input` | Parked on a permission request or a question. It needs *you*. |

```sh
xeac ps | jq -r '.[] | select(.awaiting_input) | .id'
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
xeac attach "$id" | jq -r 'select(.kind == "live") | .message.text // empty'
```

## Answering a session

When a session needs permission it parks, `awaiting_input` goes true, and `attach` emits a frame carrying the request and its id. Answer it with that id:

```
xeac approve <session> <request> [-d|--deny]
```

There is no "always allow" and no bypass mode: every decision is allow-once or deny. That is a deliberate constraint — an approval you grant once cannot silently widen into a standing grant.

## Ending a session

```
xeac kill <session>
```

Ends the session and everything under it, children first, so a child never observes a dead parent.

"Everything under it" means the session's whole **process session**, not just the worker. Each session is spawned as a process-session leader, so every shell command it ran, and everything those commands started, carries its session id — a dev server a session left holding a port goes down with the session that started it. The process *group* is the wrong unit for this and used to be what was signalled: the `bash` tool deliberately puts each command in a group of its own so that cancelling one job reaps that job's subtree, which by construction put it outside a group-wide kill.

The one way to survive is for a process to call `setsid` and leave the session, which also takes it out of everything else the harness tracks. If you want something to outlive the session that started it, that is how — and it is then yours to stop.

## Agents on other hosts

```
xeac remote                        # the registered remote agents, with their live health
xeac remote <name> <message>       # hand one a message and print what it produced
```

A remote agent is not a session: it runs on someone else's machine, at their cost, with no shared history and no access to this filesystem. That is a different bargain from a peer session, so it is a different verb — you should never be unsure which side of the wire your work went to.

Registered in `~/.agents/remote-agents.json` by card URL, or from **Settings → Remote agents**. Their cards are resolved in the background, and a card that redirects to a private or loopback address is refused unless you opt in with `allow_private` — a remote agent's own card cannot be used to point XEAC at something inside your network.

## Configuration

```
xeac configure --all                    # every setting there is, with its default
xeac configure                          # only what you have changed
xeac configure agent.permission_mode    # read one
xeac configure agent.permission_mode read_only
xeac configure agent.permission_mode --unset
```

`--all` walks the **schema**, so it lists every setting that exists — not merely the ones somebody wrote down — as a JSON object of dotted path to `{about, default, current}`. That is usually what you want: reading the file can only show the part you already know about, and a setting left at its default was otherwise invisible.

With no argument it prints a JSON object of dotted path to value for what is actually set. With a setting it prints that setting's value bare — the file's value if it has one, otherwise what the code ships with — and puts the explanation and where the value came from on stderr, so a script reading stdout never has to strip it. Values are printed as they are stored, credentials included: this reads a file you own, and deciding on your behalf what you may see of your own configuration is not this command's business.

Values are interpreted the way the file holds them: `true`, `8` and `[]` land as a boolean, a number and a list rather than as the strings your shell handed over. `null` spells null; `none` does not, because it is a real value (`workspace.strategy: none` is the default) — use `--unset` to remove a setting.

A name the schema does not define, or a value it would reject, is refused with the reason, and the file is left as it was. The daemon reads this file at startup, so an invalid value would not fail the command that set it — it would fail every command after, including the one that would put it back. A name that is merely *unknown* is worse still: it would be written, listed back, and quietly do nothing.

Changes apply to what starts **next**. See the [Configuration guide](configuration.md) for what each setting means.

## The daemon

```
xeac daemon status      # is it up, how many sessions, how many warm workers
xeac daemon start       # start it explicitly (any other command also will)
xeac daemon stop        # stop it; its sessions are reaped with it
xeac daemon endpoint    # the loopback port and capability token
```

`status` never starts anything — a status check that silently launched the service could never report the absence it was asked about. Pass `--start` if you want that.

`endpoint` prints a secret, which is why it is a verb you ask for rather than something `status` volunteers. It is what you need to point a desktop client at a daemon over SSH:

```sh
ssh workstation xeac daemon endpoint
```

## Output, exit codes, and pipes

**Everything on stdout is plumbing.** A read prints the control plane's payload as JSON; a stream prints one JSON object per line; a verb whose answer *is* a single value prints that value bare, which is what makes `id=$(xeac create …)` work. There is no formatting layer, no colour, and no `--json` flag to remember — there is nothing else it could have been. Anything that wants a table pipes to `jq`, and anything that parses this never has to guess which mode it is in.

It is minified, and every JSON object is exactly one line — no indentation, and real UTF-8 rather than `\uXXXX` escapes. Agents drive these verbs constantly and pay for indentation by the token; pipe through `jq .` when you want it laid out for a person.

Diagnostics go to stderr and outcomes go to the exit code, so neither can contaminate the data. `xeac configure some.setting` on a stderr-suppressed pipeline prints the value or nothing at all; it never prints an apology you would then have to parse around.

| Exit code | Meaning |
|-----------|---------|
| `0` | Success. |
| `1` | The call failed — no such session, the daemon is unreachable, an unknown setting. |
| `2` | The arguments were wrong (argparse). |
| `130` | Interrupted with Ctrl-C. |
| `141` | A pipe closed under it (`xeac ps \| head`). |

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

## Talking to a session directly

`xeac` reaches the daemon over its unix socket and posts every command to it, `send` included; the daemon relays to the owning session. You can also address a session yourself, which is what makes the relay a hop rather than a wall: each one serves [A2A](https://github.com/google/A2A) on `$XDG_RUNTIME_DIR/xeac/sessions/<id>.sock`, and `create` returns the capability token that authorises driving it. Discovery is open — a session's card at `/.well-known/agent-card.json` says what it is — but every other call must present the token.

That is the whole composition model. A peer is not a special kind of thing; it is a session, addressed the way you address any session.
