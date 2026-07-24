---
created: 2026-07-24T16:17:08Z
updated: 2026-07-24T20:54:21Z
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

The rename forces `src/daisy/` → `src/xeac/` and rewrites every import regardless, so the marginal cost of also moving files into folders that reflect the new architecture is low. The restructure introduces only the boundaries the architecture actually creates — a control plane, a worker, an in-worker runtime, a shared protocol layer, and the plane-agnostic foundations all three need — and does not gratuitously resplit the runtime internals, which would bury the architectural diff under rename noise and wreck `git blame`.

The spine of the layout is one rule: **the daemon never imports the runtime.** `xeacd` spawns worker processes; the workers carry the heavy runtime (LangChain, LiteLLM, model clients). That keeps the control plane light and lets the warm pool pre-fork *workers* rather than a daemon bloated with runtime imports. `base/` exists precisely so that the daemon and the CLI can share primitives with the runtime without importing it.

```
base         → (nothing internal)
protocol     → base
computer     → base
locations    → base
tools        → base, locations
runtime      → base, protocol, computer, tools, locations
worker       → base, protocol, runtime
daemon       → base, protocol, persistence          (spawns workers; does NOT import runtime)
cli          → base, protocol
rest         → base, protocol, daemon               (GUI-facing, reworked in phase 2)
```

```
src/xeac/
  base/         plane-agnostic foundations shared by every plane
  protocol/     the wire: A2A adapter, wire events, cards, addressing
  runtime/      the in-worker agent: turn loop, tools, permissions, model clients
  worker/       the per-session process that hosts a runtime and serves A2A on a socket
  daemon/       xeacd: registry, lifecycle, sole-writer persistence, brokers, warm pool
  cli/          the `xeac` command
  computer/     unchanged leaf (macOS/browser surfaces)
  locations/    unchanged leaf (local + SSH execution)
  rest/         GUI-facing REST surface (moved in stage 10, reworked in phase 2)
```

## Module map

Every module, what it holds, and where it comes from. This is the authoritative landing table for the move.

### `base/` — plane-agnostic foundations (no internal dependencies)

| File | Contains | From |
|---|---|---|
| `identifiers.py` | `new_id(prefix)` | `identifiers.py` |
| `paths.py` | XDG resolution: config/data/runtime/state/cache directories; database paths, uploads, socket paths | new + `configuration.py:23-176` |
| `configuration.py` | Config schema models: `GlobalConfiguration`, `AgentConfiguration`, MCP/Composio/A2A/Telemetry/Compaction/Workspace/Tuning configs, `BashToolConfiguration` (with its classifier), `save_api_keys` | `configuration.py` schema slice |
| `sidecar.py` | `AgentSidecar` — the mutable per-agent config writer | `configuration.py:1142-1296` |
| `prompts.py` | `PromptLoader` (`{{ var }}` markdown templates) | `configuration.py:996-1037` |
| `catalog.py` | Agent/skill/memory directory discovery, `describe_available_agents`, `list_agents`, `seed_home_agents` | `configuration.py` discovery slice |
| `skills.py` | `Skill`, `load_skills`, `skills_for_agent`, `skills_payload` | `skills.py` |
| `permission_mode.py` | `PermissionMode` with `more_restrictive` and delegation clamping | `tool_policy.py` slice |
| `tuning.py` | `Limit`, `Tuning`, `active_tuning`, `clip_to_tokens` | `tuning.py` |
| `models.py` | Model catalog: `ModelDefinition`, `find_model`, `resolve_litellm` | `models.py` |
| `providers.py` | `PROVIDERS` table, `resolve_api_key`, `resolve_base_url` | `providers.py` |
| `credentials.py` | ChatGPT token store (`load_tokens`, `valid_tokens`, `is_signed_in`), `model_is_authorized` | `chatgpt_oauth.py` store slice + `agent_internals.py:78-96` |
| `net_trust.py` | SSRF guard: `assert_public_host`/`assert_public_url`, `pin_to_ip` | `net_trust.py` |
| `telemetry.py` | OpenTelemetry facade: spans, usage counters, traceparent | `telemetry.py` |
| `sqlite_lock.py` | Async and cross-process write locks (history and background databases) | `sqlite_lock.py` |
| `message_content.py` | LangChain content-block helpers, block identities | `message_content.py` |
| `background_tasks.py` | `spawn_background_task` (strong-reference holder) | `background_tasks.py` |
| `environment_variables.py` | Environment variable name constants (`XEAC_*`) | `environment_variables.py` |

### `protocol/` — the wire

| File | Contains | From |
|---|---|---|
| `events.py` | Wire event Pydantic models, `WIRE_EVENT_MODELS` (the TypeScript generation source) | `events.py` |
| `envelopes.py` | `ModelToolResult`, `TurnContext`, tool-result envelope builders, payload capping | `events.py:268-305` + `agent_internals.py:144-236` |
| `turn_record.py` | `TurnRecord`, `TurnKind`, `PendingInteraction`, `ToolGate`, `reconcile_action` | `turn_record.py` |
| `metadata.py` | `XEAC_METADATA_KEY` (`urn:xeac:ext:turn:v1`), `Metadata` field names, envelope kinds, `PART_KIND` | `a2a_executor.py:129-209` |
| `card.py` | `build_agent_card`, session card identity | `a2a_executor.py:354-427` |
| `parts.py` | Part building (`_text_part`, `_event_part`, `_tool_result_part`) and inbound parsing (file parts, artifact events, `input_response`, image inlining) | `a2a_executor.py:211-353, 428-509` |
| `files.py` | `FileUrlSigner`, `build_file_part`, `ingest_file_part` | `a2a_files.py` |
| `handoff.py` | `build_artifact`, `build_task`, `serialize_task` | `handoff.py` |
| `errors.py` | Provider-error classification into a safe wire error | `a2a_executor.py:511-618` |
| `client.py` | Outbound A2A client: card resolution and caching, auth, host trust, `send_message` — now the one way anything talks to a session | `remote_agents.py` |
| `addressing.py` | Session socket paths, capability-token header, unix-socket transport wiring | new |

### `runtime/` — the in-worker agent

| File | Contains | From |
|---|---|---|
| `runtime.py` | `AgentRuntime` core: state, usage accounting, background-result injection, session snapshot | `agent.py:274-1040` minus delegation setters |
| `turnloop.py` | `stream`/`resume_stream`, model call and tool-batch phases | `agent_turnloop.py` minus prompt build and mailbox drains |
| `turn_events.py` | The `TurnEvent` union (`TextChunk`, `ToolCall`, `Suspended`, `Checkpoint`, …) | `turn_events.py` minus `Delegate*` |
| `internals.py` | Loop sentinels, `_stream_next`, argument coercion, timing metadata, workspace detection | `agent_internals.py` remainder |
| `compaction.py` | Observational-memory compaction; `Observation`, `ObservationBatch` | `agent_compaction.py` + `agent_internals.py:36-58` |
| `permissions.py` | Per-call classification, preflight batch, `PermissionEvaluator`, gate and plan types | `agent_permissions.py` + `configuration.py:964-990` + `agent_internals.py:356-442` |
| `locations.py` | Location resolution plus `ResolvedLocation`, `CallExecutionPolicy`, `BashAllowRule` | `agent.py:505-585` + `tool_policy.py` |
| `tasks.py` | `TaskItem`, `TaskManager` (the agent's to-do list) | `agent.py:201-271` |
| `file_leases.py` | `FileLeaseManager` (fcntl-based, cross-process for free) | `file_leases.py` |
| `annotation_stamping.py` | Numbered badges stamped onto annotated images for vision models | `annotation_stamping.py` |
| `models/factory.py` | `build_chat_model` (provider to client) | `agent.py:110-142` |
| `models/litellm.py` | `ChatLiteLLMModel` | `litellm_model.py` |
| `models/codex.py` | `ChatCodexModel` plus its plan catalog | `codex_model.py` |
| `prompt/system.py` | Static system prompt and dynamic context assembly | `agent_turnloop.py:79-166` |
| `prompt/memories.py` | Memory discovery and payload | `memories.py` |
| `prompt/instructions.py` | AGENTS.md/CLAUDE.md/CONTEXT.md loading | `instructions.py` |
| `prompt/environment.py` | Machine and user-context probes | `environment.py` |
| `tools/registry.py` | `@tool` schema declarations and roster assembly | `tools/tools.py` declarations + `agent.py:145-198` |
| `tools/dispatch.py` | `_execute_tool`, concurrent drain, validation, result append | `agent_tools.py` preamble |
| `tools/shell.py` | `bash` handler and implementation, background job producer | `agent_tools.py:523` + `tools.py:56-226` |
| `tools/filesystem.py` | read/edit/write/search_code handlers | `agent_tools.py:623,672,792` |
| `tools/file_operations.py` | read/edit/write/fetch/download implementations, syntax validation | `tools/file_tools.py` |
| `tools/code_search.py` | Semantic code search | `tools/code_search.py` |
| `tools/web.py` | fetch_url/download/search_web handlers and implementation | `agent_tools.py:719,736,1546` + `tools.py:230-313` |
| `tools/screen.py` | `control_screen` handler and script static assessment | `agent_tools.py:1599` + `agent_permissions.py:30-41` |
| `tools/mcp.py` | MCP tool and resource handlers, reaching the daemon broker | `agent_tools.py:988,1047` |
| `tools/artifacts.py` | `open_artifact` handler, artifact kind and result helpers | `agent_tools.py:1465` + `tools.py:520-595` |
| `tools/planning.py` | set_tasks, update_tasks, update_goal, read_task | `agent_tools.py:1380-1425,1564` |
| `tools/interaction.py` | `ask_user`, `load_skill` | `agent_tools.py` slices |
| `tools/sandbox.py` | Outside-working-directory read analysis | `agent_tools.py:315-373` |

### `worker/` — the per-session process

| File | Contains | From |
|---|---|---|
| `__main__.py` | Worker entrypoint: park blank (pre-forked), receive assignment, serve | new |
| `assignment.py` | The pre-fork assignment protocol: a blank worker becomes a session (agent, directory, mode, id, token) | new |
| `session.py` | The single-session executor: runtime build and cache, resume pump, autonomous turns, abort and steer, background replay | `a2a_executor.py:1534-2147` |
| `turn.py` | `_TurnRunner`: ingest, resolve, prepare, compose, stream, finalize, teardown | `a2a_executor.py:947-1533` |
| `sink.py` | `_TurnEventSink` and `_TextPartBuffer` (turn events to wire parts) | `a2a_executor.py:619-865` |
| `server.py` | A2A JSON-RPC over the unix socket, token auth, well-known card | new + `boot.py:521-548` |
| `persistence.py` | Streams turn events and checkpoints to `xeacd` instead of writing the database | new |

### `daemon/` — the control plane (never imports `runtime/`)

| File | Contains | From |
|---|---|---|
| `__main__.py` | `xeacd` entrypoint | new |
| `boot.py` | Lifespan: config load, seeding, database init, brokers, watchers, startup reconciliation, exposure guard | `server/boot.py` minus agent mounting |
| `api.py` | Control RPC: `session.create/list/get/kill/attach/history`, `task.get`, `daemon.status` | new, replacing `routes/chat.py` and `routes/sessions.py` |
| `registry.py` | Session registry (id to socket, token, status, parent), agent card registry, pending-input resolution | `a2a_executor.AgentRegistry` minus mailbox + `server/state.py` |
| `lifecycle.py` | Spawn, subtree reap, crash detection, orphaned-turn reconciliation, process-group reaping | new + `boot.py:422-431` + `background.py:474-495` |
| `pool.py` | Warm elastic worker pool (warm floor, burst ceiling, idle shrink) | new |
| `state.py` | Daemon singletons, `Broadcaster`, `ContextEventBus` | `server/state.py` daemon half |
| `watchers.py` | Configuration, agents/skills, and SSH-host file watchers | `boot.py:256-321` |
| `workspaces.py` | Session workspace strategies (none, branch, worktree) | `session_workspaces.py` |
| `persistence/ingest.py` | Receives worker event streams; the sole-writer intake | new |
| `persistence/database.py` | ORM records and additive schema application | `server/database.py` |
| `persistence/task_store.py` | `AppendOnlyTaskStore`: task head, append-only history, checkpoints, session state | `task_store.py` |
| `persistence/messages.py` | User-message recall log | `task_store.py:937-955` |
| `persistence/background_store.py` | Background job durability and process groups | `background_store.py` |
| `persistence/push_store.py` | A2A push-notification configuration store and pinned sender | `push_notification_store.py` |
| `persistence/artifacts.py` | Shadow-git capture worker (sole writer of the artifact tables), prune | `services/artifacts.py` capture half |
| `persistence/versioning.py` | Shadow-git version store | `artifact_versioning.py` |
| `brokers/mcp.py` | Shared `MCPClientManager`, live reload, per-folder ensure | `mcp_client.py` + `services/mcp.py` |
| `brokers/composio.py` | Composio to MCP server entries | `composio_router.py` |
| `brokers/chatgpt.py` | Interactive PKCE sign-in flow (single-flight) | `chatgpt_oauth.py` flow slice |
| `brokers/remote_agents.py` | External A2A peers: configuration to manager, health poll | `services/remote_agents.py` |
| `brokers/embeddings.py` | Shared model2vec and retrieval warm cache | new |
| `services/agents.py` | Card building, sidecar read and write, configuration resolution, model history | `services/agents.py` |
| `services/sessions.py` | Session listing, workspace ensure, permission-mode persistence, titles, work-habits claim | `services/sessions.py` daemon half |
| `services/projects.py` | Project and location CRUD, default project | `services/projects.py` CRUD half |
| `services/locations.py` | Location serialization and per-session resolution | `services/locations.py` |
| `services/settings.py` | Configuration persistence and reload, live credential application | `services/settings.py` |
| `services/broadcast.py` | Event fan-out to attach streams, turn and permission state | `services/broadcast.py` |

### `cli/` — the `xeac` command

| File | Contains |
|---|---|
| `__main__.py` | Entrypoint: parsing, subcommand dispatch, global flags (`--json`) |
| `autostart.py` | Docker-style daemon autostart |
| `client.py` | Control-plane RPC client and session socket client, over `protocol/client` |
| `render.py` | Human-readable output and `--json`; live event rendering for attach |
| `commands/` | One module per verb: `create`, `send`, `get`, `wait`, `attach`, `ps`, `tree`, `approve`, `kill`, `daemon`, `agents`, `config` |

### Unchanged leaves

`computer/` keeps `accessibility.py`, `control.py`, `control_child.py` (a subprocess entrypoint launched by file path, which must stay import-free of the package), `engine.py`, `input_synthesis.py`, `permissions.py`, `retrieval.py`, `surface.py`, `web.py`, plus `messages/` and `scripts/`; the only edits are the `daisy-*` thread and worker names. `locations/` keeps `executor.py` (with the XDG fix at `executor.py:351`), `location_uri.py`, `resolver.py`, and `ssh_hosts.py`.

### `rest/` — the GUI-facing surface

`app.py`; `routes/` (`artifacts`, `filesystem`, `terminals`, `mcp`, `projects`, `settings`, `sessions_ui`); `services/` (`artifacts_query`, `filesystem`, `terminals`, `proxy`); `models.py` for the DTOs. Sourced from today's `server/routes/*` and the GUI halves of `services/{artifacts,filesystem,terminals,proxy,projects,sessions,settings}.py`. The package is carved and populated during the move (stage 10) because stage 9 deletes `src/daisy/` and every module needs a home; only the *rework* into registry and session clients is deferred to phase 2.

## Slice mechanics

Four files fan out across packages and carry the real risk. For any file with one dominant destination, `git mv` it there first and then extract the minor pieces into new modules, so `git blame` follows the bulk instead of being lost to a delete-and-add.

| File | How it splits, and the trap |
|---|---|
| `a2a_executor.py` (2768 lines) | Splits across three processes: protocol (card, metadata, parts, errors), worker (`_TurnRunner`, `_TurnEventSink`, executor into `session.py`), daemon (registry, resume pump, reconciliation). The trap is that `DaisyAgentExecutor._contexts` is a dictionary keyed by context; in a worker it collapses to a single `_ContextState`. Every `self._contexts.get(...)` site must become the one session, and `_conversations` — today shared process-wide across executors so a persona switch continues the same list — becomes just this session's conversation. |
| `agent_tools.py` (1730 lines) | The handlers are mixin methods that use `self` pervasively. Split into one mixin class per family (`_ShellToolsMixin`, `_FilesystemToolsMixin`, and so on) composed into `AgentRuntime`, **not** into free functions. Converting to functions would be a rewrite disguised as a move. |
| `configuration.py` (1308 lines) | A five-way slice, except that `BashToolConfiguration` and its command classifier stay together in `base/`: the model is a nested field of the tools configuration (`configuration.py:872`) and the classifier is pure string logic with no runtime dependencies, so separating them would split a Pydantic model from its own methods and invert the layering. `AgentSidecar` (the writer) separates cleanly from the read models. |
| `agent_internals.py` (469 lines) | A deliberate grab-bag going to four destinations. It is currently a leaf by design, so splitting it between `runtime/internals.py` and `runtime/permissions.py` can create a cycle if the preflight types and the loop sentinels reference each other. Extract the types first, verify, then move the consumers. |

## Deleted

The in-process delegation and mailbox machinery, in full: the `spawn_agent` tool and its declaration, `make_delegate` and `_remote_delegate`, the `_participants` mailbox with `ask_agent`/`respond_agent`, `agent_messages.py`, `agent_delegation.py`, `agent_runner.py`, the in-process spawn branch of `_tool_spawn_or_remote`, the agent-message drains in the turn loop, and the delegation setters in `agent.py:842-940`.

The deletion reaches further than the composition code, because the agents panel existed only to surface in-process children. It also removes the `Relayed` and `GroupStarted` turn events, the `AgentPathSegment`/`AgentPath`/`AgentUsage`/`GroupStartedEvent` wire events, the `agents` bucket on `TokenUsageEvent`, `AgentLane` in `turn_record.py`, `_RELAYABLE_CHILD_KINDS`, and roughly fifty-two references across the frontend. Because the wire events are the source of the generated TypeScript, this changes `events.schema.json` and `events.ts`, and the web build gate fails until Python, schema, types, and components move together.

Also deleted: mid-session permission-mode changes (`set_permission_mode` and the `/permissions/mode` route), bypass mode, allow-always, `server/asgi.py`, `server/routes/chat.py`, `boot._mount_agent` and `boot._ensure_agents_for`, and `state._BIND_HOST`.

## Rename and placement sweep

`src/daisy/` becomes `src/xeac/` and every import follows. Beyond the imports, the load-bearing items are: every `~/.daisy` path remapped to XDG (configuration, `history.db`, `background.db`, `chatgpt_auth.json`, uploads, workspaces, the signing secret, the SSH ControlMaster directory at `locations/executor.py:351`, and the per-location `.daisy/versions` shadow-git directories, which become `.xeac/versions` and orphan the old ones on every remote host); `urn:daisy:ext:*` to `urn:xeac:*`, changed in the Python and the TypeScript in the same commit because they are one contract; the `DAISY_*` environment variables to `XEAC_*`; the `daisy-*` thread, worker, and tempfile-prefix names; `_package_version("daisy")` and the derived `USER_AGENT`, which fail at runtime rather than import time if missed; the distribution name in `pyproject.toml`; and the PyInstaller specification, which hardcodes both `collect_all("daisy")` and the `daisy/computer/<assets>` data destinations, so the frozen build silently loses the computer surfaces' assets unless it is updated.

The frozen build ships **one binary with argv dispatch** — `xeac`, `xeacd`, and the worker are the same executable entered differently. This keeps packaging to a single specification and lets the pool re-exec the binary it is already running.

## Execution order

The order is bottom-up by layer, so that at every step a moved layer depends only on layers already moved. Deletion comes first, in the old tree, where it can be verified against familiar code and where it shrinks everything that follows.

| Stage | What | Mechanics | Gate |
|---|---|---|---|
| 0. Guardrails | Symbol inventory (module to public symbols); `scripts/check_layers.py`, an AST import scan asserting the allowed edges and the no-module-level-`computer`-import rule; an import smoke script | new files only | The checker passes on the current tree with today's edges declared |
| 1. Delete delegation | The mailbox, `make_delegate`, in-process spawn, `Delegate*`, the agents-panel events on both planes, and the frontend references | delete in place, old tree | Imports resolve; `check:events` regenerated; `bun run build` passes |
| 2. `base/` | Eighteen modules; slice `configuration.py` into `paths`, `configuration`, `sidecar`, `prompts`, `catalog`; `tool_policy` into `permission_mode` | `git mv` the bulk, extract slices | `import xeac.base.*`; the layer has zero internal dependencies |
| 3. `protocol/` | `events`, `turn_record`, `handoff`, `a2a_files`; the protocol slices of `a2a_executor`; `remote_agents` into `client`; the new `addressing` | slice and extract | `import xeac.protocol.*`; protocol depends only on base |
| 4. Leaves | `computer/` and `locations/` verbatim, plus the XDG fix and the thread names | `git mv` whole | Imports resolve; `control_child.py` still launches by file path |
| 5. `runtime/` | The large `agent_*` slices, with the mixin-preserving split of the tool handlers | slice per the table above | `import xeac.runtime`; runtime depends only on base, protocol, computer, tools, locations |
| 6. `daemon/` | `boot` minus mounting, `persistence/`, `brokers/`, `services/`, registry, state, watchers; new `api`, `ingest`, `pool`, `lifecycle` | move plus new code | The daemon must not import the runtime — the checker's central assertion |
| 7. `worker/` | `_TurnRunner` into `turn`, `_TurnEventSink` into `sink`, the executor into `session`; new `__main__`, `server`, `persistence`, `assignment` | slice plus new code | `import xeac.worker`; worker depends only on base, protocol, runtime |
| 8. `cli/` | Entrypoint, client, renderer, autostart, and the command modules | new | `xeac --help` runs with no daemon present |
| 9. Rename residue | `urn:xeac:*` in both planes, `XEAC_*`, XDG paths, `pyproject` name, `_package_version`, user agent, the PyInstaller specification, the Tauri bundle identity; delete `src/daisy/` | codemod plus manual edits | Full compile; frozen-build smoke test |
| 10. `rest/` | The GUI routes and services, and the `_boot.*` bug fix | move | Imports resolve; the package no longer references `src/daisy` |

## Verification gates

There is **no test suite** — `tests/` contains zero test files despite the `pyproject` `testpaths` setting — so nothing here is verified by tests, and that is the single largest execution risk. Verification is structural, and it is what makes an irregularity visible early rather than at the first live turn: `python -m compileall src/xeac` plus a per-layer `import` smoke after each stage; the layering checker, which fails the moment a forbidden edge or a cycle appears; a symbol-inventory diff after each stage, where every public symbol recorded in stage 0 must still exist somewhere or appear on the deletion list; the existing `check:events` schema gate and the `json2ts` diff, which catch Python-to-TypeScript wire drift; and `bun run build` for the frontend. Behavioural confirmation is a manual smoke run of the daemon, one session, one turn, and one permission gate.

## Hazard register

These are the irregularities identified before starting, each with how it will be detected.

| Hazard | Why it is real | Detection and mitigation |
|---|---|---|
| Silent runtime-only breakage | No tests; most failures surface only when a turn actually runs | Structural gates catch import and shape errors; behavioural verification is a manual smoke run. Stated plainly rather than papered over |
| `daemon` importing `runtime` | It is natural to reach for `AgentRuntime` while writing `api.py`; it would defeat the light control plane and bloat the pool | The layering checker fails the build |
| A module-level `computer` import | It would make `fork()` unsafe on macOS through PyObjC and CoreFoundation. Today every import of `computer` is function-level (`agent_tools.py:1595,1609,1610`, `services/projects.py:141`, `routes/filesystem.py:106`), which is what makes pre-forking viable at all | The checker asserts lazy-only importing; this laziness is now a load-bearing invariant, not an accident |
| Python and TypeScript wire drift | `urn:daisy:ext:*` lives in both the executor and `api.ts`, and the deleted events change the generated schema | The `check:events` gate, plus changing both planes in one commit |
| Frozen-build asset loss | The specification hardcodes `collect_all("daisy")` and the `daisy/computer/<assets>` destinations | Update the specification in stage 9 and smoke the frozen binary, which the build script already exercises with a request to `/home` |
| Ordering of streamed persistence | The append-only history is row-ordered and `_persisted_counts` is tracked in memory, while workers now stream events to the daemon | The daemon remains the single writer, so ordering is preserved; a worker must never read back what it has just written |
| Symbol drop during slicing | Around one hundred and twenty destination modules; a helper can vanish unnoticed | The stage-0 inventory, diffed after every stage |
| Deletion overreach | Removing `spawn_agent` touches turn events, wire events, `turn_record`, and the frontend | Stage 1 is isolated and fully gated before any move begins |

One pre-existing defect is recorded here so it is not mistaken for migration damage: `routes/settings.py:43,49,57,63,64` and `routes/projects.py:82` call `_boot._full_disk_access_granted`, `_boot._open_full_disk_access_settings`, `_boot._accessibility_granted`, `_boot._request_accessibility`, `_boot._open_accessibility_settings`, and `_boot._project_count`, but `boot.py` defines none of them — they live in `services/projects.py`, and `boot.py` imports only `_ensure_default_project`. These are six AttributeErrors at request time today. They are fixed when those routes move in stage 10.

## Invariants

`create` is the only place configuration and permissions are set; `send` never mutates configuration. A child's mode is clamped to no looser than its parent's; there is no bypass and no allow-always, so the only runtime permission decisions are per-call allow-once and deny. A pooled worker is never reused across sessions. A session's durable state survives its process — in the database, via `xeacd` — while its socket dies with it, so reads route through `xeacd` and commands route through the socket. `xeacd` sits in the persistence and observation path, never in the inbound agent-to-agent messaging path.

Two engineering invariants join them, both enforced by the layering checker: the daemon never imports the runtime, and `computer/` is never imported at module level.

## Accepted costs

Process-per-session with per-hop A2A is intrinsically heavier than nested coroutines. The warm pool amortizes cold start, but the per-process memory floor and the per-hop latency remain. This is the deliberate price of isolation and uniformity, taken with eyes open.

Two capabilities are retired rather than reimplemented. Switching persona mid-conversation on a shared context goes away, because a session is one agent for its life and the process-wide shared conversation map goes with the monolith. The unified agents panel goes away too: a child is now its own session, observed with `xeac attach` rather than relayed into its parent's transcript.

## Phasing

1. **Core.** Stages 0 through 10: the deletions, the rename, XDG placement, the package restructure, `xeacd` with its registry, lifecycle and reaper, sole-writer persistence, brokers, warm pool and autostart, the worker process serving A2A on a token-gated socket, and the `xeac` CLI.
2. **Clients.** Rework the Tauri application and the `rest/` surface into registry and session clients.

## Open questions

The exact GUI event re-wiring, from the monolith's `ContextEventBus` and SSE to the `xeacd` registry plus session sockets, is deferred to phase 2 and planned there rather than here. Whether the home-agents layer moves fully under `$XDG_CONFIG_HOME/xeac/agents` or keeps a `~/.agents` alias for continuity with the existing dotfiles ecosystem is left open, to be decided when the configuration loader is ported.
