---
created: 2026-07-24T16:17:08Z
updated: 2026-07-24T21:19:51Z
commit: 52e5669
---

# XEAC: Sessions as Processes

This plan restructures the harness around a single primitive — the session — and renames the project from Daisy to XEAC. Everything becomes a session; a session is one OS process running one agent; agents compose by sending A2A messages to each other's sockets rather than through bespoke in-process delegation. The directive is a complete replacement with no backward compatibility. The motivation is that the harness is already, in all but name, a multi-agent A2A server whose delegation model is the special case: today a spawned agent is an in-process one-shot coroutine driven through `make_delegate`, with a parallel `_remote_delegate` path for over-the-wire agents and an in-memory `_participants` mailbox that exists only because "A2A has no RPC for injecting a peer question into a running model call." Making the session the one primitive and A2A the one composition path collapses all of that into "spawn a session, talk to its socket." The agent stops having a bespoke `spawn_agent` tool and becomes another CLI user, spawning peers exactly the way a human does; local and remote agents stop being two code paths and become one. `xeacd` is a thin control plane — registry, lifecycle, sole persistence writer, shared-resource broker, and a warm worker pool — that owns the persistence and observation path but never the inbound agent-to-agent messaging path, which stays direct and socket-to-socket.

## Thesis

The unit of durability and execution is the session: one OS process, one agent, created empty and then driven by messages over its life. `xeacd` is the control plane; the CLI (`xeac`) is ergonomic sugar over one API surface that the GUI and agents drive identically. `daisy` becomes `xeac`, `xeacd` is the daemon, wire keys move to `urn:xeac:*`, and placement moves to XDG. This is a hard break — no migration shim, no dual-running with the old layout, and, as *Execution stance* below sets out in full, no intermediate compatibility scaffolding either: the tree stays broken until the end, and the end state is the only thing ever built toward.

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
| **Human + GUI** | CLI is primary. The Tauri app and REST surface become registry and session clients, planned in full under *Frontend* and built after the backend core |
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

The layering is acyclic, and it is what the layering checker enforces:

| Package | May import |
|---|---|
| `base` | nothing internal |
| `protocol` | `base` |
| `computer`, `locations` | `base` |
| `tools` | `base`, `locations` |
| `runtime` | `base`, `protocol`, `computer`, `tools`, `locations` |
| `worker` | `base`, `protocol`, `runtime` |
| `daemon` | `base`, `protocol`, and its own `persistence` — never `runtime` |
| `cli` | `base`, `protocol` |
| `rest` | `base`, `protocol`, `daemon` |

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
  rest/         GUI-facing REST surface (moved in the REST stage, reworked in phase 2)
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

`app.py`; `routes/` (`artifacts`, `filesystem`, `terminals`, `mcp`, `projects`, `settings`, `sessions_ui`); `services/` (`artifacts_query`, `filesystem`, `terminals`, `proxy`); `models.py` for the DTOs. Sourced from today's `server/routes/*` and the GUI halves of `services/{artifacts,filesystem,terminals,proxy,projects,sessions,settings}.py`. The package is carved and populated during the REST stage because the rename stage deletes `src/daisy/` and every module needs a home; the endpoints it keeps serving are the ones the *Frontend* section maps as unchanged in shape, and they are served by the daemon's loopback listener rather than a standalone server.

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

**The repository rename is out of scope and belongs to the owner, after this work has fully landed.** This plan renames everything *inside* the tree — the package, the paths, the environment variables, the wire keys, the bundle identity, the product strings — and nothing about the repository that holds it. The GitHub repository stays `ghovax/daisy`, the remote URL and the clone path are untouched, and the working directory remains as it is for the whole migration, so the ordinary intermediate condition is a tree whose code says `xeac` inside a repository still called `daisy`; that mismatch is expected and is not a defect to correct. For the same reason the repository URLs that appear in content — the agent card's `provider` organisation and the README's links to `github.com/ghovax/daisy` — are left pointing where they point, and are updated by the owner together with the rename itself. Nothing in the execution should attempt to rename the repository, rewrite the remote, or move the checkout.

## Execution stance: one destination, no waypoints

**The tree is expected to be broken for the entire duration of the migration, and that is correct.** Nothing here is built to compile, import, or run at any intermediate point. The end state is the only target, and it is reached in one continuous motion.

This is a deliberate constraint, not an oversight, because the alternative is worse. Demanding a green tree at each step in a restructure of this size forces exactly the artefacts this plan must not produce: compatibility re-exports left behind in `src/daisy/` so old imports keep resolving, facade modules keeping `a2a_executor.py` alive while its pieces move out from under it, stub functions standing in for handlers that have not moved yet, adapter shims bridging the old monolith to the half-built daemon, and dual code paths kept alive side by side so neither breaks. Every one of those is written to be deleted, and every one of them distorts the design it is scaffolding around. Work aimed at an intermediate green state is work aimed at the wrong target.

The rules that follow from this, and they are absolute:

**Never write a shim, stub, facade, adapter, or compatibility re-export.** If a module's callers have not been migrated yet, leave them broken; they are migrated later in the same continuous pass. **Never keep an old path alive alongside a new one.** There is no dual-running, no deprecation window, no fallback branch — the old code is deleted the moment its replacement is written, not after it is proven. **Never soften a design decision to make an intermediate state work.** If the destination shape is right, build the destination shape and let everything that references the old shape stay broken until it is rewritten. **Never repair a break in code that is scheduled to be rewritten or deleted anyway** — fixing an import in a module that the next stage removes is pure waste.

Verification happens once, at the very end, and only then. Until that point, a red tree carries no information and should be neither consulted nor chased.

## Execution order

The stages run in the order listed. The order is bottom-up by layer, but this is now purely for the author's coherence — knowing where `base` symbols live before writing the runtime that imports them reduces churn and rework. It is emphatically **not** a sequence of checkpoints, and no stage is expected to leave the tree in a working condition. Deletion comes first because it shrinks everything that follows, and the guardrails inventory is captured first because the baseline it records cannot be reconstructed once the move has begun.

| Stage | What | Mechanics |
|---|---|---|
| **Guardrails** | Baseline symbol inventory (module to public symbols), captured before anything moves; `scripts/check_layers.py`, an AST import scan asserting the allowed edges and the no-module-level-`computer`-import rule. Both are tools for the final verification, not gates to pass now | new files only |
| **Deletion** | The mailbox, `make_delegate`, in-process spawn, `Delegate*`, the agents-panel events on both planes, and the frontend references | delete in place, old tree |
| **Foundations** | `base/`: eighteen modules; slice `configuration.py` into `paths`, `configuration`, `sidecar`, `prompts`, `catalog`; `tool_policy` into `permission_mode` | `git mv` the bulk, extract slices |
| **Protocol** | `events`, `turn_record`, `handoff`, `a2a_files`; the protocol slices of `a2a_executor`; `remote_agents` into `client`; the new `addressing` | slice and extract |
| **Leaves** | `computer/` and `locations/` verbatim, plus the XDG fix and the thread names | `git mv` whole |
| **Runtime** | The large `agent_*` slices, with the mixin-preserving split of the tool handlers | slice per the table above |
| **Daemon** | `boot` minus mounting, `persistence/`, `brokers/`, `services/`, registry, state, watchers; new `api`, `ingest`, `pool`, `lifecycle` | move plus new code |
| **Worker** | `_TurnRunner` into `turn`, `_TurnEventSink` into `sink`, the executor into `session`; new `__main__`, `server`, `persistence`, `assignment` | slice plus new code |
| **CLI** | Entrypoint, client, renderer, autostart, and the command modules | new |
| **Rename** | `urn:xeac:*` in both planes, `XEAC_*`, XDG paths, `pyproject` name, `_package_version`, user agent, the PyInstaller specification, the Tauri bundle identity; delete `src/daisy/` | codemod plus manual edits |
| **REST** | The GUI routes and services, and the `_boot.*` bug fix | move |

## Verification, once, at the end

Only after the final stage does anything get run. There is no test suite to inherit — `tests/` contains zero test files despite the `pyproject` `testpaths` setting — so the end-state pass is built from scratch, in this order.

First the tree is made to compile: `python -m compileall src/xeac`, then an import of every package, fixing whatever falls out. Because nothing was checked along the way, this is where the accumulated breakage surfaces, and it surfaces all at once; that is the accepted price of the stance above and it should be worked through as one focused debugging pass rather than treated as a surprise.

Then the structural checks confirm the restructure did what it claimed: the layering checker, asserting the acyclic layering and both engineering invariants; and the symbol-inventory diff against the baseline captured in the guardrails stage, where every public symbol must either still exist somewhere or appear on the deletion list. Then the cross-plane gates: the `check:events` schema regeneration, the `json2ts` diff, and `bun run build`.

Finally, behaviour is proven with **throwaway tests** — written at this point and only this point, for the sole purpose of demonstrating that the end state works. They cover the paths that matter: the daemon starts and its control API answers; a session is created, assigned a warm worker, and serves A2A on its token-gated socket; a message drives a turn end to end; a permission gate suspends the session durably and the answer resumes it; a peer session is created from inside an agent and reached over the wire; and a parent's death reaps its children. These are scaffolding, not a deliverable — they exist to validate the migration and are discarded once it is proven. Building a real suite is worthwhile, but it is a separate piece of work with a separate goal, and conflating the two would pull focus off the destination.

## Hazard register

These are the irregularities identified before starting, each with how it will be detected. Detection happens during the final verification pass, not along the way; the value of listing them now is that they are watched for while writing, so they are recognised rather than rediscovered.

| Hazard | Why it is real | Detection and mitigation |
|---|---|---|
| Silent runtime-only breakage | No inherited tests; most failures surface only when a turn actually runs | The throwaway tests at the end are written precisely to force these paths to execute. Stated plainly rather than papered over |
| `daemon` importing `runtime` | It is natural to reach for `AgentRuntime` while writing `api.py`; it would defeat the light control plane and bloat the pool | Held as a rule while writing the daemon, and confirmed by the layering checker at the end |
| A module-level `computer` import | It would make `fork()` unsafe on macOS through PyObjC and CoreFoundation. Today every import of `computer` is function-level (`agent_tools.py:1595,1609,1610`, `services/projects.py:141`, `routes/filesystem.py:106`), which is what makes pre-forking viable at all | The checker asserts lazy-only importing; this laziness is now a load-bearing invariant, not an accident |
| Python and TypeScript wire drift | `urn:daisy:ext:*` lives in both the executor and `api.ts`, and the deleted events change the generated schema | Both planes are changed together as a matter of discipline; the `check:events` gate confirms it at the end |
| Frozen-build asset loss | The specification hardcodes `collect_all("daisy")` and the `daisy/computer/<assets>` destinations | Update the specification during the rename stage and smoke the frozen binary, which the build script already exercises with a request to `/home` |
| Ordering of streamed persistence | The append-only history is row-ordered and `_persisted_counts` is tracked in memory, while workers now stream events to the daemon | The daemon remains the single writer, so ordering is preserved; a worker must never read back what it has just written |
| Symbol drop during slicing | Around one hundred and twenty destination modules; a helper can vanish unnoticed | The baseline inventory, diffed at the end |
| Deletion overreach | Removing `spawn_agent` touches turn events, wire events, `turn_record`, and the frontend | The deletion stage is done in one pass against the familiar old tree, before any module has moved |

One pre-existing defect is recorded here so it is not mistaken for migration damage: `routes/settings.py:43,49,57,63,64` and `routes/projects.py:82` call `_boot._full_disk_access_granted`, `_boot._open_full_disk_access_settings`, `_boot._accessibility_granted`, `_boot._request_accessibility`, `_boot._open_accessibility_settings`, and `_boot._project_count`, but `boot.py` defines none of them — they live in `services/projects.py`, and `boot.py` imports only `_ensure_default_project`. These are six AttributeErrors at request time today. They are fixed when those routes move during the REST stage.

## Frontend

The frontend is not an afterthought to this migration and not a string-replacement exercise. The desktop client drives a monolith over REST and SSE at a fixed `localhost:8822`, renders spawned agents in a panel that is being deleted, and is packaged by a Rust shell that spawns exactly one server process and proves it healthy by curling a TCP port. All three of those assumptions die. What follows plans the client with the same care as the backend, and it is executed under the same stance: no shims, no dual paths, no intermediate green.

### Where it stands

The client is a static Next.js export (`output: "export"`) inside a Tauri shell, and its weight is concentrated in four files: `chat-panel.tsx` (2258 lines, the monolith that owns `useChat`, the transcript timeline, the artifact panel, and the overlays), `tool-views/index.tsx` (2136 lines, every per-tool renderer), `use-chat.ts` (2267 lines, the turn state machine and event reducer), and `api.ts` (1665 lines, roughly seventy exported functions over REST, SSE, A2A, and one WebSocket). Everything reaches the backend through a single mutable `API_BASE` string.

### The transport decision

**A webview cannot open a unix socket.** There is no `tauri-plugin-http`, no registered URI scheme, no asset-protocol handler, and no occurrence of `AF_UNIX` or `XDG_RUNTIME_DIR` anywhere in the repository; the client's only transports are `fetch`, `EventSource`, and `WebSocket`, all of which require an `http(s)`/`ws(s)` origin. So while the CLI and agents reach `xeacd` and session sockets directly over the runtime directory, the GUI cannot.

Therefore **`xeacd` also serves its control API on a loopback TCP listener for GUI clients**, gated by the same capability token, which the Tauri shell reads from a `0600` file in the runtime directory and attaches to every request. This is what keeps the client's entire `fetch`/`EventSource` architecture intact; the alternative — proxying every call through Tauri IPC — would mean rewriting all seventy `api.ts` functions and reimplementing SSE and WebSocket semantics over Tauri events, for no gain. It also, finally, closes the standing hole where the REST surface had no authentication at all and relied purely on binding to loopback.

Because the GUI cannot reach session sockets either, **the daemon relays GUI data-plane commands to the owning session's socket.** This does not violate the invariant, which reserves the daemon out of the *agent-to-agent* messaging path; a command from a human's client is not that. The CLI keeps talking to session sockets directly, so the two clients differ in transport while sharing one API.

### Endpoint remapping

Every call the client makes today lands somewhere new. The mapping is the specification for the `api.ts` rewrite.

| Today | Becomes |
|---|---|
| `POST /a2a/agents/{agent}` (drive a turn, SSE) | `session.create` once, then `send` per turn, relayed to the session socket |
| `GET /sessions/{id}/stream` (observe) | `session.attach` |
| `GET /sessions`, `GET /sessions/{id}/tasks[/page]` | `session.list`, `session.history` |
| `DELETE /sessions/{id}` | `session.kill` (reaps the subtree) |
| `POST /chat/{id}/permission`, `/question` | `send` carrying an `input_response` part |
| `POST /chat/{id}/steer` | `send` — steering *is* a safe-point-injected message now, so the separate endpoint disappears |
| `POST /chat/{id}/abort`, `/tools/{id}/abort` | `tasks/cancel` on the session socket |
| `POST /chat/{id}/permissions/mode` | **deleted** — the mode is fixed at `create` |
| `POST /chat/{id}/agents/{taskId}/abort` | **deleted** — a child is a session; `session.kill` covers it |
| `GET /events` (shared bus), settings, projects, agents, models, artifacts, terminals, filesystem | daemon control API, unchanged in shape |

### Component fates

| Component | Fate |
|---|---|
| `agents-panel.tsx` (188), `agent-timeline.tsx` (69) | **Deleted whole** — the panel exists only to render in-process spawned agents |
| `chat-panel.tsx` (2258) | Rewired to `create`/`send`/`attach`; loses `activeSteps` (`:786-789`), the agent-lane permission fallback (`:1252-1277`), the auto-open effect (`:1321-1330`), the Agents toolbar button (`:1396-1406`), and the `<AgentsPanel>` mount (`:2101-2113`) |
| `use-chat.ts` (2267) | Loses the entire lane machinery — `AgentGroup`/`AgentStep`, `ensureLaneGroup`, `reduceAgentLaneEvent`, the `path`-based relay routing with its `event_id` dedup, `isImmediateAgentEventMessage`, the child-task replay closure, and the agents token bucket. The two stream paths collapse into `send` plus `attach` |
| `tool-views/index.tsx` (2136) | Loses `SpawnAgentCallView`, `AskAgentCallView`, `RespondAgentCallView` (`:120-166`), `AgentMessageResultView` (`:777-801`), `AgentTaskResultView` (`:849-863`) and their switch entries |
| `permission-overlay.tsx` | **Two buttons, not three** — allow-always is gone, so the `1`/`2`/`3` keyboard map becomes `1`/`2` (`:42-60`, `:141-151`) |
| `chat-input.tsx` | Loses the agent token-usage block (`:179-190`); the permission-mode chip becomes a *creation-time* choice, not a live toggle |
| `session-controls.tsx` | `PermissionModeControl` moves from a live control to a session-creation control |
| `background-tasks-panel.tsx` | Loses the `spawn_agent` job kind (`:64-76`) |
| `sessions-sidebar.tsx` | **Gains a tree** (see below) |
| `tool-display.ts` | Loses the `spawn_agent`/`cancel_agent`/`ask_agent`/`respond_agent` entries, labels, and five icon imports |
| `app/gallery/page.tsx` | Loses the `spawn_agent` fixture (`:132-138`) and the agent-step section (`:267-280`) |
| Everything else (artifacts, terminals, settings, projects, locations, connection, `ui/`) | Survives; naming only |

### A consequence that needs new UI

Deleting the agents panel does not delete the concept — it relocates it. A spawned agent is now a **session**, so a task that fans out five children puts five new rows in the sidebar, indistinguishable from the user's own conversations. The sidebar must therefore render the parent/child hierarchy that `session.tree` exposes, with children nested under the session that spawned them and collapsed by default. Without that, the deletion trades a contained panel for a cluttered sidebar, which is a worse result than what exists today. This is the one place where the frontend needs genuinely new design rather than rewiring, and it is why the agents panel cannot simply be removed and forgotten.

### Naming sweep

| Category | Items |
|---|---|
| Storage keys | `daisy.apiBase`, `daisy.connections`, `daisy.appState`, `daisy.sessionConnections`, `daisy.locale`, `daisy.reconnect`, `daisy:lastProject`, `daisy:pendingComputerControlEnable` |
| Wire keys | `urn:daisy:ext:turn:v1`, `urn:daisy:ext:content-block:v1` — changed in the same commit as the Python |
| Environment | `NEXT_PUBLIC_DAISY_API_BASE`, `DAISY_PORT` (the Next dev-server port, *not* 8822 — easy to conflate) |
| Tauri events / tags | `daisy://new-chat`, `daisy://open-session` (Tauri event names that merely look like a scheme — nothing registers them with the OS), `daisy-permission-`, `daisy-notification-click` |
| CSS / assets | `.daisy-terminal-surface` (nine sites plus its one consumer), the `daisyIcon` imports and wordmarks in `sessions-sidebar.tsx` and `connection-settings.tsx` |
| Product strings | `layout.tsx` title and description, tray labels, the two user-facing error copies in `api.ts` and their duplicate in `use-chat.ts` |
| Generated | the `DaisyEvents` schema title and interface, and the `daisy-events.check.ts` temp filename in the `check:events` script |
| i18n | Eleven "Daisy" values in each of `en.json` and `ja.json` at identical line numbers, plus the whole `AgentsPanel` namespace (17 keys) and the agent entries under `ToolViews`/`ToolDisplay`. `ja.json` is a verified exact mirror of `en.json` (647 keys, 29 namespaces), so both move together or the catalogs desync |

One cross-plane trap: the four `SessionControls` workspace strings hardcode the branch prefix **`daisy/session`**, which the backend produces and `ja.json` keeps untranslated. Renaming either side alone leaves the UI describing a branch that does not exist.

### Desktop shell and packaging

The Rust shell assumes one server, one port, one pid. All three change.

`spawn_local_server` passes `argv = [path, NULL]` (`lib.rs:79`) — there is no slot for a subcommand, so argv dispatch requires changing the spawn itself to launch the daemon entrypoint. `LocalServer` holds a single pid and `kill_local_server` sends one `SIGTERM` with no process-group handling, so a daemon's worker tree would be orphaned on quit; the shell must kill the process group and the daemon must reap its own children. `local_port_open` currently does triple duty — is the backend up, did someone else start it, is this a crash orphan — and that logic inverts once a socket file is involved, since a stale socket outlives a crash; readiness becomes a connect-and-probe against the daemon's loopback port, with the daemon unlinking its own stale socket at startup. The SSH tunnel stays TCP (`-L` to the daemon's loopback port, with `sshRemotePort` persisted in SQLite migration 003 and six UI sites), which is another reason the loopback listener earns its place.

**The build gate blocks everything and must be fixed first.** `packaging/build-sidecar.sh:42-61` launches the frozen binary with **no arguments** and polls `curl http://127.0.0.1:8822/home` up to forty times, failing the build if nothing answers. A daemon that needs an argv subcommand, or that binds only a socket, fails there before the app ever compiles. The smoke test becomes: launch with the daemon subcommand, probe the daemon's readiness endpoint on its loopback port. The freshness guard at `:25-33` also hardcodes the watched source list (`src/daisy`), so the new package layout must be added or the freeze silently no-ops — a failure mode that looks like a stale build rather than an error.

**The macOS code-identity requirement is load-bearing and easy to break.** The `Daisy Computer Use.app` helper shares the app's bundle identifier and signing identity precisely so the server process's Accessibility grant folds into the app's single entry — and workers are exactly the processes that will run the computer-use tools. Only one helper is ever signed (`sign-app.sh:42`). Therefore **workers must be spawned as the same signed executable inside the same bundle** — a re-exec or fork of the daemon binary, never another path or a bare interpreter — or macOS will list each worker as a separate Accessibility entry and ask the user to grant permission per worker. The one-binary-with-argv-dispatch choice satisfies this, but only because it is stated as a requirement here rather than left to chance.

The remaining packaging renames are mechanical: the `EXE`/`COLLECT`/`BUNDLE` names and identifier in the spec, the helper `.app` name and paths across `build-sidecar.sh`, `sign-app.sh`, and `lib.rs:146-157`, the `"Daisy Local Codesign"` identity and the certificate script, `Cargo.toml`'s `name`/`default-run`/`[[bin]]`/`[lib]` (where the capitalised binary name is load-bearing for the Accessibility prompt), `tauri.conf.json`'s product name, identifier, and window title, the five TCC usage strings in `Info.plist`, the `Daisy.icon` composer document, and `package.json`'s `daisy-web`. `Entitlements.plist`'s `disable-library-validation` stays — it exists for the frozen helper.

### Frontend stages

These run after the backend core, in this order, under the same stance — nothing is expected to work until the end. The delegation deletion is not listed here because it happens on both planes together in the backend's deletion stage.

| Stage | What |
|---|---|
| **Client transport** | Rewrite `api.ts` against the daemon control API and the relayed data plane; token attachment; the endpoint remapping above |
| **Session rewire** | `use-chat.ts` and `chat-panel.tsx`: collapse the two stream paths into `send` plus `attach`, strip the lane machinery, move permission mode to creation |
| **Sidebar tree** | Render the parent/child session hierarchy from `session.tree` |
| **Desktop shell** | `lib.rs` daemon lifecycle, process-group kill, readiness probe, SSH tunnel port; the build-sidecar smoke test and freshness list; signing and bundle identity |
| **Frontend naming** | Storage keys, wire keys, environment, Tauri events, CSS, assets, product strings, generated schema title, both i18n catalogs |

### Frontend hazards

| Hazard | Why it is real | Mitigation |
|---|---|---|
| Build gate fails before anything else | The sidecar smoke test curls `:8822/home` against an argument-less launch; it runs inside `tauri build` | Fix the smoke test in the desktop-shell stage, before attempting any app build |
| Per-worker Accessibility prompts | Workers run the computer-use tools; only one helper is signed | Workers are re-execs of the same signed binary inside the same bundle — stated as a requirement, not an accident |
| Orphaned worker tree on quit | The shell kills one pid with one `SIGTERM` | Kill the process group; the daemon reaps its children |
| i18n catalog desync | `ja.json` mirrors `en.json` exactly today | Both catalogs edited together; key counts compared at the end |
| The `daisy/session` branch prefix | Produced by the backend, hardcoded in four UI strings, untranslated in `ja.json` | Renamed on both planes in the same commit |
| Sidebar flooded by child sessions | Deleting the agents panel relocates children into the session list | The sidebar tree stage is part of the plan, not an optional follow-up |
| Two app instances racing | No single-instance plugin; both can pass the port probe and write the pid stamp | Pre-existing; the daemon's socket-and-lock ownership makes it recoverable, and it is recorded rather than silently inherited |

## Invariants

`create` is the only place configuration and permissions are set; `send` never mutates configuration. A child's mode is clamped to no looser than its parent's; there is no bypass and no allow-always, so the only runtime permission decisions are per-call allow-once and deny. A pooled worker is never reused across sessions. A session's durable state survives its process — in the database, via `xeacd` — while its socket dies with it, so reads route through `xeacd` and commands route through the socket. `xeacd` sits in the persistence and observation path, never in the inbound agent-to-agent messaging path.

Two engineering invariants join them, both confirmed by the layering checker in the final pass: the daemon never imports the runtime, and `computer/` is never imported at module level.

## Accepted costs

Process-per-session with per-hop A2A is intrinsically heavier than nested coroutines. The warm pool amortizes cold start, but the per-process memory floor and the per-hop latency remain. This is the deliberate price of isolation and uniformity, taken with eyes open.

Deferring all verification to the end has its own price, accepted deliberately: every accumulated error surfaces at once, in a tree that has changed everywhere, which is a harder debugging position than catching each break as it appears. It is taken in exchange for never once bending the design toward a temporary green state, and for never writing a line whose only purpose is to be deleted later.

Two capabilities are retired rather than reimplemented. Switching persona mid-conversation on a shared context goes away, because a session is one agent for its life and the process-wide shared conversation map goes with the monolith. The unified agents panel goes away too: a child is now its own session, observed with `xeac attach` rather than relayed into its parent's transcript.

## Phasing

1. **Core.** Every stage in the backend table: the deletions, the rename, XDG placement, the package restructure, `xeacd` with its registry, lifecycle and reaper, sole-writer persistence, brokers, warm pool and autostart, the worker process serving A2A on a token-gated socket, and the `xeac` CLI.
2. **Clients.** Every stage in the frontend table: the client transport rewrite, the session rewire, the sidebar tree, the desktop shell, and the frontend naming sweep.

Both phases are fully specified here. The split is an ordering of work, not a difference in how completely each is planned, and the execution stance applies across both: verification happens once, after the frontend stages, when the whole system is expected to run for the first time.

## Open questions

Whether the home-agents layer moves fully under `$XDG_CONFIG_HOME/xeac/agents` or keeps a `~/.agents` alias for continuity with the existing dotfiles ecosystem is left open, to be decided when the configuration loader is ported.
