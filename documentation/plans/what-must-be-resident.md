---
created: 2026-07-27T16:27:43Z
updated: 2026-07-27T16:27:43Z
commit: 98560a9
---

# What Must Be Resident

The daemon is not the problem. *Process-per-session-for-life* is the problem, and the confinement design — which is where that decision is usually assumed to come from — has nothing to do with it.

A session worker is not sandboxed. Its **tool children** are. `_apply_posix` is a `preexec_fn` (`base/confinement.py:210`), `sandbox-exec` prefixes the shell command (`runtime/tools/registry.py:131`), and Landlock restricts the child after fork. The worker process itself runs with the user's full privileges. So the process boundary between two sessions is crash isolation, an address, and a `killpg` unit — not a security boundary. N sessions in one process would be confined byte-for-byte identically.

Once that is clear, "do we need a server?" stops being one question and becomes two independent ones. **Who must be resident?** — nobody, the client that created the sessions, or a machine-wide daemon. **What does a session cost?** — an object, a copy-on-write fork, a cold process held for life, or a durable record with a process only while it works. Today is the most expensive cell on both axes at once: a machine-wide daemon *and* a cold 264 MB process held for a session's entire life, idle or not.

This plan keeps the daemon, because two use cases earn it and nothing else can serve them — a session that outlives the terminal that started it, and a harness reached from another machine. It makes everything else cheaper: the runtime becomes re-entrant, workers become copies instead of cold starts, an idle session stops being a process, and the resident thing splits into the part that supervises processes and the part that serves a browser.

Measured, not assumed. The numbers below are from the real import graph on Python 3.13, and where they could not be measured on the target platform this document says so and ships the probe.

## What the measurements say

Every figure is from `daisy.worker.session` — the actual parked-worker import graph, 1,240 heavyweight modules — on Linux x86-64, Python 3.13.7, against proportional set size, which is the only honest accounting when pages are shared.

| Measurement | Result |
|---|---|
| Parked worker, resident | **263.8 MB**, 5.1 s import warm-cache (10.5 s cold), 3,443 modules |
| Threads in a parked worker | **One.** LangChain, LiteLLM, httpx, tiktoken, SQLAlchemy, a2a and tree-sitter start no background threads |
| Forked child, immediately | 1.5 MB private |
| Forked child, after building a real `AgentRuntime` | 12.1 MB private, 25 tools |
| Forked child, after heavy churn and repeated full collections | **110.3 MB** private without mitigation; **13.6 MB** with `gc.freeze()` |
| Time from request to a session serving on its own socket | **54–125 ms**, median ≈ 60 ms (12 sessions × 3 repetitions, identical every run) |
| Twelve-session fan-out | **406 MB** by fork vs **3,433 MB** modelled by spawn — **88.2 %** |
| Marginal cost of one more session | **264 MB → ≈ 12 MB**, and **≈ 5 s → ≈ 60 ms** |

Fork was verified against the hazards that actually apply, not the folklore ones. A child that makes its own event loop, calls `setsid`, binds a unix socket, spawns a subprocess and logs: all pass. A child that reuses the parent's `ThreadPoolExecutor`, or touches a lock another thread held at fork time: both fail, exactly as they should. Those two failures are the entire risk, and the parked worker's own import graph creates neither — the only thread in the process is the one `worker/__main__.py:37` creates itself.

Three conditions follow, and all three are satisfiable:

1. **The forking parent must be single-threaded.** CPython 3.13 emits a `DeprecationWarning` on `fork()` from a multi-threaded process, and it is right to. Forking from a single-threaded parent produced no warning. Today's park mechanism — `run_in_executor(None, sys.stdin.readline)` — makes the parent multi-threaded for no reason; a blocking `accept()` needs no thread and no loop.
2. **`gc.freeze()` before forking is mandatory, not an optimisation.** Without it the first cyclic collection walks every tracked object, dirties its page, and un-shares 98 MB. With it, the decay does not happen at all.
3. **The child must take its own signals and lead its own process session.** `runtime/tools/registry.py:855` installs process-wide `SIGTERM`/`SIGHUP` handlers *at import time* that call `sys.exit(1)`. Today that is masked because the worker's entry point overrides it; it is a landmine either way, and it is deleted here.

The daemon must never import the runtime, so **the daemon cannot be the forking parent.** That is what forces a third process, and one consequence with it: the daemon can no longer `waitpid` a worker it did not fork, so worker deaths are reported rather than waited on.

## The prototype

A **prototype** is a resident process that has paid the runtime import once, frozen its heap, and parks. It runs no agent and serves no API. Asked for a session, it forks; the child calls `setsid`, restores default signal handling, binds the session's socket, and becomes that session. The general mechanism is what CPython's `multiprocessing` calls a *forkserver*; the name here is `prototype` because what it is, is the image every worker is a copy of.

```
daisyd  ──spawns──▶  prototype  ──forks──▶  session worker
 14.8 MB              263.8 MB               ≈ 12 MB
 no runtime           runtime + gc.freeze()  setsid, own socket, own signals
```

The warm pool is deleted rather than reworked. A pool of pre-started workers exists to hide a five-second import; a fork has no import to hide, so a floor, a ceiling, a top-up task and a spawn-on-empty fallback are all answers to a question that no longer arises.

A prototype is not a supervisor of policy — it holds no registry, makes no decisions, and knows nothing about permission modes or trees. It does one thing the daemon cannot do for itself, and it reports exits because it is the only process in a position to observe them.

## Three boundaries

Everything below follows from separating three concerns that are currently one process.

| Boundary | What it owns | Why it is a boundary |
|---|---|---|
| **Supervision** | Registry, lifecycle, prototype, kernel caller attribution, ingest | Must be resident. This is what a session's existence depends on, and it should be small enough to read in one sitting |
| **The turn record** | `history.db`: turn head, history, artifacts, checkpoints, session state | Genuinely single-writer — `_persisted_counts` is an in-memory, lock-guarded counter (`persistence/turn_store.py:296`) and the append-only design rests on it |
| **Workspace state** | `workspace.db`: projects, locations, artifacts, terminals, model history, drafts, user messages | Multi-writer-safe under the `fcntl` locks that already exist. It has no supervision relationship to anything, and it is what the browser talks to |

One database file forced one writer on both records. Two files put the boundary where the reason is.

## Part I — The runtime becomes re-entrant

First, because Parts II, III and IV each depend on it, and because one of its items is a live bug.

| # | Change | Where | Why |
|---|---|---|---|
| 1 | Delete `set_confinement`/`active_confinement`; `dispatch` reads `self._sandbox` | `runtime/tools/registry.py:60-71`, `dispatch.py:565-568,1440` | `dispatch.py:565-568` **mutates the process-global confinement profile mid-turn** to repoint the workspace for one `bash` call. A worker can legitimately have two turns open at once — `ingest.py:74-76` says so, for compaction and autonomous wakes — so this is a confinement race today. The field already exists at `runtime.py:363`; the global never needed to |
| 2 | Delete `set_exa_client`, `set_jina_api_key`, `set_firecrawl_client`, `set_proxy_url`, `set_mcp_client_manager`; build all five in `_build_tools` from `self._global_configuration` | `runtime/tools/registry.py:22,40,45`, `file_operations.py:52-71`, `worker/session.py:729-763` | These are not installed capabilities, they are configuration, and the runtime already holds the configuration. Reading them from a global is a monolith habit that makes a settings change half-applicable |
| 3 | `set_session_access` becomes a constructor argument; the session tools are built per-runtime | `runtime/tools/sessions.py:91-97,256-315` | `SessionAccess` was designed as an injected `Protocol` (`sessions.py:71`) and then installed globally anyway. `build_create_session_tool` already builds per-runtime; the other five follow it |
| 4 | `set_tuning`'s `_active` becomes a `ContextVar` | `base/tuning.py:464` | The pattern is already in this file at line 44 and in `background.py:498`. This is the odd one out |
| 5 | **Delete the module-level `signal.signal()` and `atexit` registration** from the tool registry; the worker entry point owns its signals | `runtime/tools/registry.py:849-856` | A library module that hijacks `SIGTERM` process-wide makes every embedder's shutdown wrong. Found by forking: the handler fires in the child and exits it |
| 6 | Publish the library surface: `daisy.Session(agent, directory, sandbox, …)` over an in-process `SessionAccess` | new `src/daisy/__init__.py` | After 1–5 `runtime/` is genuinely re-entrant. It already imports nothing from `daemon` or `worker`; it has simply never had a front door. This is what lets a terminal interface and a browser interface share one harness — which is the requirement that produced the server in the first place |
| 7 | The layering checker gains a rule: no module-level `signal.signal`, `atexit.register`, or mutable module-level state under `runtime/` | `scripts/check_layers.py` | Items 1–5 are a class, not five incidents. Nothing else stops the class from coming back |

## Part II — Workers are copies, not cold starts

| # | Change | Where | Why |
|---|---|---|---|
| 8 | Add the `prototype` entry point: import the runtime, `gc.collect()`, `gc.freeze()`, park on a blocking `accept()` — no event loop, no threads | new `daemon/prototype.py`, `src/daisy/__main__.py` | Condition 1. A blocking accept keeps the parent single-threaded, which is what makes the fork legal |
| 9 | The forked child `setsid`s, restores `SIG_DFL`, binds its socket, then reports ready on a pipe | `worker/__main__.py` | `setsid` is what `peer_identity.session_for_process` depends on; verified 12/12 leaders. Ready-after-bind is what stops a `send` racing the bind, exactly as `lifecycle._await_ready` does today |
| 10 | **Delete `daemon/pool.py`** — floor, ceiling, top-up, spawn-on-empty, `worker_command`, `release` | `daemon/pool.py`, `base/configuration.py` (`daemon.warm_floor`, `daemon.warm_ceiling`) | At 60 ms there is nothing left to pre-warm. The two configuration keys go with it |
| 11 | The prototype reaps its own children and reports each exit to the daemon; `lifecycle._supervise` consumes reports instead of `await process.wait()` | `daemon/lifecycle.py:150`, `daemon/prototype.py` | The daemon cannot `waitpid` a process it did not fork. `pidfd_open`/`kqueue` would also work; reporting needs no platform-specific syscall and is about a hundred lines |
| 12 | The daemon supervises the prototype and restarts it if it dies; a dead prototype does not affect live sessions | `daemon/lifecycle.py` | Sessions are independent processes and are reparented. Only *new* sessions block, and only until the restart finishes |
| 13 | The prototype is a re-exec of the same signed image, as the worker is today | `src/daisy/__main__.py` | The macOS Accessibility grant follows code identity. A fork inherits the parent's signature, so the fleet stays one TCC row — the property `pool.py:60` exists to protect |
| 14 | Correct the documentation: nothing forks today (`pool.py:146` is `create_subprocess_exec`) | `architecture.md:51`, `documentation/README.md:40`, `development.md:53`, `worker/__main__.py:13`, `daemon/__main__.py:9`, `base/browser_assets.py:6`, `rest/services/filesystem.py:401` | Six documents and three docstrings describe a mechanism that has never existed. The `computer`-is-lazily-imported invariant is real and load-bearing — it just protects a fork that was never taken |

## Part III — A session is a record, not a process

| # | Change | Where | Why |
|---|---|---|---|
| 15 | Collapse the two `SessionRecord`s into one durable table | `daemon/registry.py:35` and `persistence/database.py:20` | Two classes with the same name, overlapping fields, neither complete. `daisy ps` reads the volatile one; the browser lists from the durable one |
| 16 | Session tokens are **derived**, not stored: `HKDF(master_key, session_id)` | `daemon/registry.py:128` | A durable registry must hand a token to a *new* worker on wake, which means it must be recomputable. Deriving stores nothing at rest. The precedent is `FileUrlSigner(load_or_create_secret(...))` in `protocol/files.py` |
| 17 | `status` splits: a durable `lifecycle` (`live`/`ended`) and a derived `activity` (`working`/`idle`/`asleep`) | `registry.py:26-31`, `api.py:113` | The registry already cannot answer "is it working" — `_public()` merges `busy` from `_running_contexts` for exactly this reason. Make the split explicit rather than patched |
| 18 | `relay_to_session` becomes `wake_then_relay`: no worker, ask the prototype, replay the assignment, then relay | `daemon/state.py:172` | **This is the whole mechanism, and it is one function.** Every command already funnels through it — `session.send`, `respond`, `compact`, `jobs.*`, `turn.cancel`. Reads (`get`, `list`, `tree`, `history`, `attach`) already bypass it and must keep doing so |
| 19 | Sleep an idle worker: no turn in flight, no pending background jobs, after a linger window | `daemon/lifecycle.py`, reading `state._running_contexts` | **A session parked on a permission prompt is the clearest case.** `input-required` is already a durable suspension (`worker/session.py:628`) — its entire state is on disk and it holds a whole interpreter to wait. Background jobs are the one real blocker: they are in-process `asyncio.Task`s |
| 20 | Sessions survive a daemon restart | `daemon/api.py:477` | `daemon.restart` exists solely for the macOS TCC cache and currently documents "Every live session ends". With a durable registry it stops being true, and the confirmation dialog goes with it |
| 21 | The isolation invariant is restated, not weakened: **one worker, one session, one activation** | `daemon/prototype.py` docstring | Workers are never recycled. A slept session's next worker is a fresh copy of the prototype. Nothing in `xeac-migration.md:31` changes |

## Part IV — The resident thing splits in two

| # | Change | Where | Why |
|---|---|---|---|
| 22 | Split the database. `history.db`: turn head, history, artifacts, checkpoint, session state, registry. `workspace.db`: projects, locations, `artifact_*`, terminal states, model history, user messages, drafts | `base/paths.py`, `persistence/database.py`, `persistence/turn_store.py` | The sole-writer invariant is real for one and imaginary for the other. `base/background_store.py` already proves a worker can own a second database under the existing `fcntl` lock |
| 23 | The browser backend becomes a **client of the daemon**, serving the 72 REST endpoints against `workspace.db` itself and proxying only the control plane, `attach` and `/events` | `rest/` moves under the `daisy web` process | `cli/commands/web.py` already built this shape and then pointed it at the daemon. A crash in the artifact previewer must not reap agents. `client-and-daemon.md` argued the app must not own *the harness*; a backend that owns no sessions and spawns no agents is not that |
| 24 | The daemon shrinks to registry, lifecycle, prototype, `peer_identity`, `ingest`, `api`, `state` | `daemon/` ≈ 6,996 → ≈ 2,000 lines | This is the process every session's existence depends on |
| 25 | **Rewire artifact capture into the worker**, writing `workspace.db` under the existing lock | `worker/session.py:133`, `persistence/artifacts.py` | `state.capture_queue` is never assigned and `_capture_worker` is never started, so `_enqueue_capture` returns at `artifacts.py:75` every time. 1,078 lines and 11 endpoints serve permanently empty tables. The worker already holds the filesystem the shadow-git operates on |
| 26 | **Rewire file leases**: the manager moves to `base/` as a process-free façade over the `fcntl` locks it already uses | `base/file_leases.py:186`, `worker/session.py:129` | `_file_lease_manager` is `None` in every worker, so `_acquire_filesystem_lease` returns `""` and **no session has ever taken a lease**. `_lock_os_key` is already cross-process `flock`; the manager object was only ever the in-process fast path |
| 27 | **Rewire location resolution**: locations travel in the assignment, resolved at `session.create` | `daemon/api.py:196`, `worker/session.py:132` | `_resolve_locations` is `None`, so every runtime synthesises one local location and multi-location projects are dead at the session level. Same treatment the sandbox and the workspace already get — resolved once, carried with the session (`confinement.md:48`) |
| 28 | The layering table changes: `rest` no longer imports `daemon`; it reaches the control plane over HTTP like any other client | `scripts/check_layers.py` | The exemption that let `rest` import `runtime` for three ChatGPT endpoints also disappears — that surface moves with the browser backend |

## Deleted

| What | Where | Why it goes |
|---|---|---|
| The warm worker pool, its floor, ceiling and both configuration keys | `daemon/pool.py`, `base/configuration.py` | 60 ms leaves nothing to pre-warm |
| Eight module-global setters and their read sites | `runtime/tools/registry.py`, `file_operations.py`, `sessions.py`, `base/tuning.py` | Configuration and per-runtime state pretending to be process state |
| The module-level `signal.signal()` and `atexit.register()` | `runtime/tools/registry.py:849-856` | A library must not seize a process's signals |
| `_on_new_context`, `_session_permission_mode_for`, `_ensure_mcp_servers`, `_ensure_session_workspace`, `_claim_persisted_work_habits_acknowledgement` | `worker/session.py:122-131` | Genuinely superseded: the mode is fixed at create, the workspace is resolved at create, MCP is per-session |
| `services/sessions.py:_claim_work_habits_acknowledgement` and `SessionLifecycleRecord` | `daemon/services/sessions.py:21`, `persistence/database.py:56` | No caller; the worker keeps its own in-memory flag |
| The duplicate `SessionRecord` | `daemon/registry.py:35` | Merged into the durable table |
| `DaemonTurnStore`'s artifact path and the daemon's REST surface | `worker/turn_store.py`, `daemon/` | Moved to the boundary that owns them |

## What is deliberately not changing

The daemon stays. Two use cases earn it and nothing else serves them: a session that outlives the terminal that started it, and a harness reached from another machine over a URL or an SSH tunnel. A daemonless design in the shape of Podman is genuinely attractive and fails both — and it strands `peer_identity`, because with no daemon socket there is no kernel-attested peer and the permission clamp falls back to the tokens that module was written to stop trusting.

Sessions stay processes. The XEAC migration's prize was crash isolation for a harness whose composition story is fan-out; a peer that runs out of memory must not take its parent and siblings with it. Part III removes the cost of that isolation without removing the isolation.

The confinement design is untouched. It was never the reason for process-per-session and it is not affected by any of this.

`ask_user`, `daisy send --wait`, the subtree scoping on every control-plane verb, and the permission clamp all keep their present behaviour.

## Invariants

`create` remains the only place a session's configuration is set, and a child is still clamped to no looser a mode than its parent. A worker still serves exactly one session and is never recycled — a slept session's next worker is a fresh copy of the prototype, not a reused one. The daemon still never imports the runtime, which is now doubly load-bearing: it is what keeps the control plane light *and* what keeps the prototype, rather than the daemon, the thing that forks. `computer/` is still never imported at module level, and that invariant stops being theoretical: it is what keeps CoreFoundation out of the prototype's address space, which is what makes forking legal on macOS at all.

Two invariants join them. Nothing under `runtime/` holds mutable module-level state, registers an `atexit` hook, or installs a signal handler. And the prototype is single-threaded at the moment it forks.

## Accepted costs

There are three resident processes where there were one and a bit: the daemon, the prototype, and the browser backend when a browser is open. The prototype's 264 MB is paid whether or not a session is running — the same money the warm pool spends today on two parked workers, for one process instead of two, and with no ceiling on how many sessions it can serve.

Waking a slept session pays an MCP reconnect, which for a stdio server spawned through `uvx` can be seconds. The linger window exists to make that rare rather than to make it fast.

A session's capability token becomes recomputable from a master key rather than existing only in RAM. That is a real widening — anything that can read the master key can mint any session's token — bounded by the fact that the same reader could already read the daemon's own token from the same 0600 directory.

## Hazard register

| Hazard | Why it is real | Detection |
|---|---|---|
| **CoreFoundation after a fork, on macOS** | macOS aborts a process that calls into CF after a fork if CF was initialised before it. The lazy-`computer` invariant means the prototype never initialises it, so the child initialises it fresh — which should be legal and is the pattern Android's runtime uses. **This is the load-bearing unknown and cannot be checked from anywhere but a Mac** | `scripts/probe_fork_macos.py`, test 2. It also reports whether any CF module reached the prototype's import set at all, which is the condition that would kill the design outright |
| **TCC does not follow a fork** | A forked child has the parent's executable, signature and responsible process, so the Accessibility grant should be inherited — plausibly better than a re-exec | Same probe, test 3: the child's `AXIsProcessTrusted()` compared with the parent's |
| **A dependency pulls PyObjC at import** | The invariant covers `daisy.computer`; it cannot cover a third-party package that imports a framework on darwin | Same probe, the module census printed before the tests |
| **A future dependency starts a thread at import** | The measurement holds for today's graph. A background thread in the prototype reintroduces the whole fork hazard class | The prototype asserts `threading.active_count() == 1` before it forks, and refuses rather than forking unsafely |
| **`gc.freeze()` is forgotten or reverted** | Without it the saving drops from 88 % to roughly a third of that, silently — nothing breaks, memory just grows | The prototype records its frozen count in `daemon.status`; a zero is visible |
| **Sleeping a session with background work** | A background job is an in-process `asyncio.Task`; sleeping its worker kills it | The idle test includes `has_pending_jobs()`. `background_store` already models the loss if it is ever wrong |
| **Two writers to `workspace.db`** | The split makes the worker and the browser backend both writers | They already share `base/sqlite_lock.py`'s cross-process `flock`; `background.db` has worked this way all along |
| **A partially-migrated registry** | Two registries becoming one touches `ps`, the sidebar, the reaper and attribution together | They are merged in one change; there is no interval in which both exist |

## Left open

**MCP stdio servers still run unconfined,** unchanged from `confinement.md`, and now with a second edge: each session connects its own, so a slept and woken session respawns them. Whether an MCP server should carry its own declared profile is still the open question it was.

**Background work still dies with its worker.** Part III makes that visible rather than solving it: the honest fix is a detached job that outlives the session and reports through the daemon, which is a subsystem, not a line. It is deliberately not in this plan.

**Terminals remain the browser backend's,** which means a terminal dies when that backend restarts. It is the one piece of state that genuinely wants a resident host and does not obviously belong to either boundary.

**The Linux measurements are the ones that exist.** Everything in this plan is proven on Linux against the real import graph and unproven on macOS, which is the platform Daisy targets. The probe is the gate, and it should be run before any of Part II is written.
