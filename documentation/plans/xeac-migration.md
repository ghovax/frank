---
created: 2026-07-24T16:17:08Z
updated: 2026-07-24T16:17:08Z
commit: 52e5669
---

# XEAC: Sessions as Processes

This plan restructures the harness around a single primitive — the session — and renames the project from Daisy to XEAC. Everything becomes a session; a session is one OS process running one agent; agents compose by sending A2A messages to each other's sockets rather than through bespoke in-process delegation. The directive is a complete replacement with no backward compatibility. The motivation is that the harness is already, in all but name, a multi-agent A2A server whose delegation model is the special case: today a spawned agent is an in-process one-shot coroutine driven through `make_delegate`, with a parallel `_remote_delegate` path for over-the-wire agents and an in-memory `_participants` mailbox that exists only because "A2A has no RPC for injecting a peer question into a running model call." Making the session the one primitive and A2A the one composition path collapses all of that into "spawn a session, talk to its socket." The agent stops having a bespoke `spawn_agent` tool and becomes another CLI user, spawning peers exactly the way a human does; local and remote agents stop being two code paths and become one. `xeacd` is a thin control plane — registry, lifecycle, sole persistence writer, shared-resource broker, and a warm worker pool — that owns the persistence and observation path but never the inbound agent-to-agent messaging path, which stays direct and socket-to-socket.

## Thesis

The unit of durability and execution is the session: one OS process, one agent, created empty and then driven by messages over its life. `xeacd` is the control plane; the CLI (`xeac`) is ergonomic sugar over one API surface that the GUI and agents drive identically. `daisy` becomes `xeac`, `xeacd` is the daemon, wire keys move to `urn:xeac:*`, and placement moves to XDG. This is a hard break — no migration shim, no dual-running with the old layout.

## Architecture

| Concern | Committed choice |
|---|---|
| **Naming** | `daisy`→`xeac`, daemon `xeacd`, wire keys `urn:xeac:*`, XDG placement (below). Hard break, no shim |
| **Unit of execution** | Session = one OS process, one agent. Created empty (`create`), then driven by messages (`send`) over its life |
| **Control plane** | `xeacd`: registry (id→address/status/parent) + lifecycle/reaper + sole DB writer + shared broker (MCP, ChatGPT OAuth, embeddings, telemetry) + warm elastic worker pool (generic pre-forked; warm floor, bursts to a ceiling, shrinks when idle). Docker-style autostart on any `xeac` command |
| **Data plane** | Each session exposes A2A over a unix socket (TCP opt-in only when remote-reachable). Commands go in direct, socket-to-socket; reads and live events come out via `xeacd` |
| **Composition** | Delete `spawn_agent`, `make_delegate`, `_remote_delegate`, the in-memory mailbox. An agent spawns a peer with `xeac create`+`send` via bash; `ask` collapses into async `send` |
| **Reaping** | Parent session ends → its children are reaped. Services and blackboards are therefore top-level sessions (owned by the human or `xeacd`) |
| **Permissions** | Mode fixed at `create`, immutable; child clamped ≤ parent. Bypass removed, allow-always removed. Runtime decisions are only per-call allow-once / deny |
| **Security** | Each session socket requires a capability token minted at `create` and handed only to the creator — a session is reachable only by who holds its handle |
| **Peer messaging** | `send` to a live session is safe-point injected (delivered at the next tool boundary); async, so there is no mutual-await block; notify = A2A push-notifications; blackboard = a top-level session |
| **Persistence** | `xeacd` is the sole writer — workers stream turn events to it; single-writer preserves today's append-only store with no cross-process lock contention |
| **Remote/SSH** | Worker runs locally, reaches the remote over the wire, as today. No installs on remote hosts |
| **Human + GUI** | CLI is primary. The Tauri app and REST surface are a phase-2 migration to registry+session clients — planned, built after the core |
| **Worker pool** | A pooled worker is assigned once, becomes that session for life, dies when reaped — never recycled. Isolation stays free |

## API surface

The CLI is sugar over two planes. Commands go in to a session's own socket (direct, peer-to-peer); reads and live events come out through `xeacd`, which is the sole writer and therefore already holds everything, whether the session is alive or reaped. An agent talking to a peer and a human driving a session use the identical data-plane calls — `message/send` to the target's socket — so the CLI, the GUI, and agents call exactly the same thing. The data plane is largely today's A2A executor surface; the genuinely new build is `xeacd` plus turning each session into a socket-served process.

### Control plane — `xeacd` (`$XDG_RUNTIME_DIR/xeac/xeacd.sock`): lifecycle, registry, all reads

| Operation | Call | Purpose |
|---|---|---|
| Create session | `session.create {agent, dir, mode, parent}` → `{id, socket, token}` | Mint the context and assign a warm worker. The single config point |
| List / tree | `session.list {filter?}` · `session.tree {id}` | The registry — powers `ps`/`tree` |
| Get session | `session.get {id}` → `{status, agent, parent, socket, awaiting}` | Resolve a session's address/status |
| Read a turn | `task.get {id, task?}` → status + result artifact | Served from the persister — works alive or reaped |
| Read history | `session.history {id}` | Replay a session's turns |
| Attach (live) | `session.attach {id}` (event stream) | The fan-out hub for live viewing |
| Kill | `session.kill {id}` | Terminate and reap the subtree |
| Daemon status | `daemon.status` | Health, pool size, session counts |

### Data plane — a session's own socket (`$XDG_RUNTIME_DIR/xeac/sessions/<id>.sock`), standard A2A, direct to the worker (token required)

| Operation | A2A method | Purpose |
|---|---|---|
| Send a message | `message/send {parts}` → task handle | New turn; safe-point injected if the session is mid-turn |
| Answer a gate | `message/send` + `input_response` part | Approve / deny / answer — unblocks the live worker |
| Cancel a turn | `tasks/cancel {taskId}` | Abort in-flight work |
| Discovery | `GET /.well-known/agent-card.json` | The session's agent card (A2A standard, kept) |

## Placement (XDG, everywhere including macOS)

Placement follows the XDG Base Directory convention rather than a single `~/.xeac` dotdir, everywhere, respecting the `XDG_*` environment variables and falling back to the standard defaults. Sockets go in `XDG_RUNTIME_DIR` specifically because that is what it is for — per-user tmpfs, reaped by the OS on logout — so there is no stale-socket cleanup to own.

| Concern | Location |
|---|---|
| Config (`configuration.yaml`, home agents) | `$XDG_CONFIG_HOME/xeac/` (`~/.config/xeac/`) |
| Durable state (`history.db`, `background.db`, `uploads/`, signing secret, workspaces) | `$XDG_DATA_HOME/xeac/` (`~/.local/share/xeac/`) |
| Sockets (`xeacd.sock`, `sessions/<id>.sock`) | `$XDG_RUNTIME_DIR/xeac/` — OS-reaped on logout |
| Logs, pidfiles | `$XDG_STATE_HOME/xeac/` (`~/.local/state/xeac/`) |
| Regenerable caches (embeddings) | `$XDG_CACHE_HOME/xeac/` (`~/.cache/xeac/`) |

Project-local `.agents` stays project-relative and unchanged. The `/.well-known/agent-card.json` A2A discovery path is kept for per-session network discovery. There is no fleet-catalog endpoint.

## Package structure

The rename forces `src/daisy/` → `src/xeac/` and rewrites every import regardless, so the marginal cost of also moving files into folders that reflect the new architecture is low. The restructure introduces only the boundaries the architecture actually creates — a control plane, a worker, an in-worker runtime, and a shared protocol layer — and does not gratuitously resplit the runtime internals, which would bury the architectural diff under rename noise and wreck `git blame`.

```
src/xeac/
  cli/         # the `xeac` command — thin client over the two API planes
  daemon/      # xeacd: registry, lifecycle/reaper, sole-writer persistence, broker, warm pool   (from today's server/)
  worker/      # the per-session process: boots a runtime, serves A2A on its socket               (new entrypoint)
  runtime/     # the agent runtime that runs inside a worker: turn loop, tools, permissions, model (today's core/)
  protocol/    # A2A adapter, wire events, card building — shared by worker (serve) and daemon/cli (consume)
  computer/    tools/    locations/     # largely unchanged
```

## Invariants

`create` is the only place config and permissions are set; `send` never mutates config. A child's mode is clamped to no looser than its parent's; there is no bypass and no allow-always, so the only runtime permission decisions are per-call allow-once and deny. A pooled worker is never reused across sessions. A session's durable state survives its process — in the DB, via `xeacd` — while its socket dies with it, so reads route through `xeacd` and commands route through the socket. `xeacd` sits in the persistence and observation path, never in the inbound agent-to-agent messaging path.

## Deleted

`spawn_agent` tool; `make_delegate` and `_remote_delegate`; the `_participants` mailbox with `ask_agent`/`respond_agent`; mid-session permission-mode changes (`set_permission_mode` and the `/permissions/mode` route); bypass mode; allow-always.

## Phasing

1. **Core.** Rename, XDG placement, and the package restructure; `xeacd` (registry, lifecycle/reaper, sole-writer persistence, broker, warm pool, autostart); the worker process serving A2A on a token-gated socket; the `xeac` CLI; and the deletions above.
2. **Clients.** Migrate the Tauri app and trim the REST surface to registry and session clients.

## Accepted costs

Process-per-session with per-hop A2A is intrinsically heavier than nested coroutines. The warm pool amortizes cold start, but the per-process memory floor and the per-hop latency remain. This is the deliberate price of isolation and uniformity, taken with eyes open.

## Open questions

The exact GUI event re-wiring from the monolith's `ContextEventBus`/SSE to the `xeacd` registry plus session sockets is deferred to phase 2 and planned there, not here. Whether the home-agents layer moves fully under `$XDG_CONFIG_HOME/xeac/agents` or keeps a `~/.agents` alias for continuity with the existing dotfiles ecosystem is left open, to be decided when the config loader is ported.
