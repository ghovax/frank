# Architecture

Daisy is one executable entered three ways. `daisy` is the command a person runs, `daisyd` is the daemon, and a **worker** is what the daemon re-execs to become a session. They are the same image rather than three binaries for two reasons: packaging stays a single specification, and a worker launched as a re-exec carries the same code identity as the signed application bundle — which is what keeps one macOS Accessibility grant covering every session instead of prompting once per worker.

```mermaid
flowchart LR
    subgraph Clients
        Cli["daisy (CLI)"]
        App["Desktop app<br/>(Tauri + Next.js)"]
        Peer["Another session"]
    end

    subgraph Daemon["daisyd — the control plane"]
        Registry["Session registry"]
        Lifecycle["Lifecycle + reaper"]
        Stores["Sole writer:<br/>history.db"]
    end

    Prototype["Prototype — the runtime,<br/>imported once and frozen"]

    subgraph Session["A session — one OS process"]
        Executor["Agent loop<br/>(LangChain)"]
        Permissions["Permission engine"]
        Tools["Tools: shell, files, web,<br/>screen control, MCP"]
    end

    ModelProvider["Model provider<br/>(Anthropic, OpenAI, … via LiteLLM)"]

    Cli -->|unix socket| Daemon
    App -->|loopback TCP + token| Daemon
    Peer -->|unix socket| Daemon
    Daemon --> Registry & Lifecycle & Stores
    Lifecycle -->|asks it to fork| Prototype
    Prototype -->|fork| Session
    Prototype -->|reports each exit| Lifecycle
    Daemon -->|relays A2A to its socket| Session
    Session -->|writes through the daemon| Stores
    Executor --> Permissions --> Tools
    Executor <--> ModelProvider
```

## Sessions

A **session** is a durable record, with an OS process only while it is working. It is created empty and then driven by messages over its life — creation and work are separate steps, so the same session can be sent a second task, attached to, and inspected between them.

The process is an activity, not the session. An idle session is put to sleep immediately: its worker is stopped, its record and its conversation stay, and the next message forks it a new worker from the prototype in about 60 ms. There is deliberately no linger window — at that price, keeping a 12 MB interpreter alive on the chance that a message arrives is paying continuously to avoid paying occasionally. The clearest case is a session parked on a permission prompt: the suspension is already fully on disk, and holding an interpreter to wait for a person who may take hours bought nothing.

Two consequences follow. A daemon restart ends every session's *process* and no session at all. And the capability token is derived from the session id rather than stored, because a woken session has to be handed the same token its creator was given.

Each session serves [A2A](https://github.com/google/A2A) (JSON-RPC) on **its own unix socket** in the runtime directory, and the daemon is what talks to it. Every client — the terminal, the desktop app, another session — reaches the daemon and the daemon relays, so there is one place where a caller is identified, scoped to its own subtree, and recorded. A session's socket being real and addressable is what makes that relay a thin hop rather than a reimplementation, but nothing bypasses it today.

There is no in-process delegation — a session that needs a peer creates one through the same control plane a person's client calls, using its `create_session` tool, and the peer answers by messaging it back. A child appears in `daisy ps`, can be attached to, and is reaped when its parent ends.

Isolation is a property of the process. A process becomes one session and stays that session for the rest of its life; it is never reused and never serves a second session, so there is no path by which one session's state can reach another's. That holds under forking too — a fork is a copy of the prototype, which has run no agent and holds no session state, never a copy of another session.

## The daemon

`daisyd` is deliberately thin — it runs no agents, and it never imports the runtime. It owns:

- the **registry** of sessions (identity, parent, permission mode, capability token, status);
- the **lifecycle**: asking the prototype to fork a session, hearing about crashes, and reaping a subtree parent-last so a child never outlives its parent;
- the **databases**, as the sole writer — workers persist by posting to the daemon's ingest surface, so there is exactly one process writing SQLite;
- the shared **brokers**: events, terminals, file leases, workspaces, signed file URLs, push notifications, and remote agents — everything there can only sensibly be one of;
- the **prototype**, which it starts, supervises and restarts. That is a separate process rather than a part of the daemon for a reason the layering makes unavoidable: whatever forks a session must already have imported the runtime, and the daemon must never import the runtime. If the prototype dies, live sessions are untouched — they are independent processes — and only new sessions wait, for as long as the restart takes.

It serves one API two ways: a **unix socket** for the CLI and for sessions, and a **loopback TCP port** for the desktop client, which cannot open a unix socket from a webview. The port is ephemeral and chosen at boot; both listeners require the capability token the daemon writes `0600` into the runtime directory.

A token says a caller may drive the daemon; it does not say *who* is calling, and on the unix socket that distinction is load-bearing. A session's own `bash` tool runs as the same user and can read that `0600` file, so attribution resting on tokens alone would have let a session present the daemon's token and be handed a peer with no parent and no permission clamp. So the unix listener asks the kernel instead: `SO_PEERCRED` (Linux) or `LOCAL_PEERPID` (macOS) names the process that opened the connection, and because every worker is started as a process-session leader, `getsid` on that pid names the session it belongs to — for the worker itself and for every shell command and `daisy` invocation underneath it. That answer wins over the token, so a session is itself whatever token it holds, and a caller the kernel places in no session (a person's terminal, the desktop client) is unattributed as it should be. That same session id is what `daisy kill` signals, so the two answers are one fact read in two directions: what the kernel calls a session is what the harness attributes to it and what it reaps with it. The way out is for a caller to `setsid` itself, which leaves the session entirely — it stops being the session rather than escaping as it, and it is no longer identified, no longer scoped, and no longer reaped.

## The CLI

`daisy` adds nothing the control plane does not have — it is the ergonomic face of it. `create` a session, `send` it work, `ps` what is running, `attach` to watch, `tree` to see what created what, `approve` a pending tool call, `kill` a subtree, `remote` to reach an agent on another host, `configure` what the next session starts with. The [CLI guide](cli.md) is the reference.

Everything goes to the daemon, `send` included — `daisy` opens the daemon's unix socket and posts to `/rpc`, and the daemon relays to the owning session. One path, so a call is attributed and scoped in exactly one place whoever made it.

## The app

A [Tauri](https://tauri.app) shell around a [Next.js](https://nextjs.org) UI (static export; Chakra UI). It is a **client** — it holds no agent logic. It renders conversations, manages settings, and chooses which daemon to talk to.

Because a webview cannot open a unix socket, the app uses the daemon's loopback listener and the daemon relays data-plane commands to the owning session.

The app does not contain a daemon and does not start one. It finds one — reading the port and token `daisyd` publishes into the runtime directory — and is powerless when there is none, exactly as it is when a remote host does not answer. "Local" is a label for the daemon on this machine, not a different mechanism: connecting to it is the same act as connecting over a tunnel, minus the tunnel. The daemon is a separate installable (`packaging/build-daemon.sh`), signed with the same identity as the app so the two share one macOS Accessibility grant. `daisy open` brings the daemon up and launches the window in one command, which is the dependency running from the command line to the app rather than the other way round.

## Connections: local, remote, SSH

A daemon's address and its token belong together — each `daisyd` mints its own token at boot, so a remote daemon does not accept the local one. A saved connection profile therefore carries both. The client resolves, in order:

1. a connection you activated in **Settings → Connections** (its URL and its token), then
2. the endpoint the desktop shell reports for the local daemon, then
3. the build-time default `NEXT_PUBLIC_DAISY_API_BASE`, then
4. the conventional local address.

That yields three ways to run:

- **Local (default).** The app starts and manages `daisyd` on this machine and reads its token from the runtime directory.
- **Remote URL.** Run `daisyd` on another host, expose its loopback port behind your own transport security, and add the URL plus the token. The app becomes a native front-end to a remote backend — the agent's shell, files, and network all live on that host.
- **Over SSH.** Add an SSH host; Daisy forwards a local port to the daemon's port on the remote, so the harness can live on a machine you only reach over SSH with nothing exposed.

For the last two, run `daisy daemon endpoint` on that host: it reports the port and the token the connection needs.

Keeping the halves apart serves one goal: **put the compute, the files, and the credentials wherever they belong, and keep the interface native and local.**

## Permissions

A session's permission mode is fixed when it is created and cannot be changed afterwards, and a child is clamped to no looser a mode than its parent. There is no bypass mode and no standing "always allow"; the only runtime decisions are allow-once and deny. See [Security notes](../SECURITY.md).

## Request lifecycle (a message)

1. You send a message to a session — `daisy send` writes to its socket directly; the app posts to the daemon, which relays it.
2. The agent loop calls the model, which may request tool calls.
3. Each tool call is classified for risk and checked against the session's permission mode. If it needs approval, the session streams a permission request; the CLI prints it and `daisy approve` answers, or the app shows an overlay.
4. Approved tools run — shell in an OS-enforced confinement (`sandbox-exec` on macOS, Landlock on Linux) resolved when the session was created and clamped against its creator, files on the active location, screen control (`control_screen`) against the local machine, MCP against the session's own connections (stateful connections and stdio subprocesses do not cross a process boundary, so a session connects its own rather than sharing the daemon's).
5. Results stream back as structured events. The session posts them to the daemon, which is the only writer of `history.db`, and fans them out to whoever is attached.

## Where to go next

- Configure providers and behavior: [Configuration guide](configuration.md).
- Author agents, skills, memory, and MCP servers: [Agents and skills guide](agents-and-skills.md).
- The tool surface in detail: [Tools guide](tools.md).
