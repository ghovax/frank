# The `xeac` command

`xeac` is the primary way to drive XEAC. It adds nothing the API does not have — it is the ergonomic face of it — so anything you can do here you can also do from the desktop app or from another session.

The daemon starts itself on your first command. There is no separate "start the service" step.

## The shape of it

A **session** is one OS process running one agent. You create it empty, send it work, and read what it produced. Creating and working are separate steps on purpose: the same session takes a second task, can be attached to, and can be inspected in between.

```sh
id=$(xeac create --agent general-assistant --directory ~/code/project)
xeac send "$id" "what does this project do?" --wait
xeac ps
```

`create` prints the bare session id on stdout, which is what makes `id=$(xeac create …)` work — including from inside an agent's own `bash` tool, which is how a session creates a peer.

## Creating a session

```
xeac create [-a AGENT] [-C DIRECTORY] [-m MODE] [-p PROJECT] [-P PARENT] [-t TITLE]
```

| Flag | What it does |
|------|--------------|
| `-a`, `--agent` | The agent profile to run. Defaults to `default_agent` from the configuration. |
| `-C`, `--directory` | The working directory. Project-local agents, skills and MCP servers are resolved from here. |
| `-m`, `--mode` | `default`, `auto`, or `read_only`. Fixed for the session's life. |
| `-p`, `--project` | The project this session belongs to. |
| `-P`, `--parent` | The session creating this one. The child is clamped to no looser a mode than its parent, and is reaped when the parent ends. |
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

`send` returns as soon as the message is accepted, printing the task id. With `--wait` it follows the session until it goes idle and prints the deliverable — what the session produced, not its transcript.

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

`ps` shows a state per session, in the order you need it:

| State | Meaning |
|-------|---------|
| `waiting` | Parked on a permission request or a question. It needs *you*. |
| `working` | A turn is in flight. |
| `idle` | Alive, with nothing in flight. Send it something. |
| `starting` | Created; its socket is not yet accepting connections. |
| `exited` / `failed` | Over. `--all` shows these; `xeac get` says why. |

`working` and `idle` are both a live process — a session's process outlives its turns, so "running" alone could not tell them apart.

`attach` prints the session's prose as it streams, plus what it is doing and anything it needs from you. It ends when the session does; interrupt it with Ctrl-C to stop watching without affecting the session.

## Answering a session

When a session needs permission, `attach` prints the request and the exact command to answer it:

```
! needs approval: rm -rf build/
  xeac approve session-1a2b… req-7f3c
```

```
xeac approve <session> <request> [-d|--deny]
```

There is no "always allow" and no bypass mode: every decision is allow-once or deny. That is a deliberate constraint — an approval you grant once cannot silently widen into a standing grant.

## Ending a session

```
xeac kill <session>
```

Ends the session and everything under it, children first, so a child never observes a dead parent. Each session leads its own process group, so a session that started a dev server takes that dev server with it.

## Peers on other hosts

```
xeac remote                        # the registered peers, with their live health
xeac remote <name> <message>       # hand one a message and print what it produced
```

A remote agent is not a session: it runs on someone else's machine, at their cost, with no shared history and no access to this filesystem. That is a different bargain from a local peer, so it is a different verb — you should never be unsure which side of the wire your work went to.

Registered in `~/.agents/remote-agents.json` by card URL, or from **Settings → Remote agents**. Their cards are resolved in the background, and a card that redirects to a private or loopback address is refused unless you opt in with `allow_private` — a peer's own card cannot be used to point XEAC at something inside your network.

## Configuration

```
xeac configure                          # everything, as dotted paths
xeac configure agent.permission_mode    # read one
xeac configure agent.permission_mode read_only
xeac configure agent.permission_mode --unset
```

Values are interpreted the way the file holds them: `true`, `8` and `[]` land as a boolean, a number and a list rather than as the strings your shell handed over. `null` spells null; `none` does not, because it is a real value (`workspace.strategy: none` is the default) — use `--unset` to remove a setting. Secrets are masked in listings, so a configuration dump you paste into an issue does not leak an API key.

A value the schema would reject is refused with the reason, and the file is left as it was. The daemon reads this file at startup, so an invalid value would not fail the command that set it — it would fail every command after, including the one that would put it back.

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

## JSON, exit codes, and pipes

Every command takes `-j`/`--json`, before or after the verb, and emits the raw payload instead of formatted output. Anything scripted should read that rather than parsing the human format, which is free to change.

```sh
xeac ps --json | jq -r '.[] | select(.awaiting_input) | .id'
```

| Exit code | Meaning |
|-----------|---------|
| `0` | Success. |
| `1` | The call failed — no such session, the daemon is unreachable, an unknown setting. |
| `2` | The arguments were wrong (argparse). |
| `130` | Interrupted with Ctrl-C. |
| `141` | A pipe closed under it (`xeac ps \| head`). |

## Talking to a session directly

`xeac` reaches the daemon over its unix socket, and a data-plane command goes straight to the owning session's own socket. You can do the same thing yourself: each session serves [A2A](https://github.com/google/A2A) on `$XDG_RUNTIME_DIR/xeac/sessions/<id>.sock`, and `create` returns the capability token that authorises driving it. Discovery is open — a session's card at `/.well-known/agent-card.json` says what it is — but every other call must present the token.

That is the whole composition model. A peer is not a special kind of thing; it is a session, addressed the way you address any session.
