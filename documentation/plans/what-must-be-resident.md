---
created: 2026-07-27T16:27:43Z
updated: 2026-07-27T19:35:00Z
commit: 38537c6
---

# What Must Be Resident

The daemon is not the problem. *Process-per-session-for-life* is the problem, and the confinement design — which is where that decision is usually assumed to come from — has nothing to do with it.

A session worker is not sandboxed. Its **tool children** are. `_apply_posix` is a `preexec_fn` (`base/confinement.py:210`), `sandbox-exec` prefixes the shell command (`runtime/tools/registry.py:131`), and Landlock restricts the child after fork. The worker process itself runs with the user's full privileges. So the process boundary between two sessions is crash isolation, an address, and a `killpg` unit — not a security boundary. N sessions in one process would be confined byte-for-byte identically.

Once that is clear, "do we need a server?" stops being one question and becomes two independent ones. **Who must be resident?** — nobody, the client that created the sessions, or a machine-wide daemon. **What does a session cost?** — an object, a copy-on-write fork, a cold process held for life, or a durable record with a process only while it works. Today is the most expensive cell on both axes at once: a machine-wide daemon *and* a cold 264 MB process held for a session's entire life, idle or not.

This plan keeps the daemon, because two use cases earn it and nothing else can serve them — a session that outlives the terminal that started it, and a harness reached from another machine. It makes everything else cheaper: the runtime becomes re-entrant, workers become copies instead of cold starts, an idle session stops being a process the moment it goes idle, and the browser surface stops depending on the thing that supervises agents.

And it finishes the job re-entrancy starts. A runtime that can run twice in one process is still not embeddable while every durable thing it writes goes to a path we chose — a library session that runs one background command creates a SQLite database under the caller's data directory today, and there is no argument that stops it. Part VII turns each of those into an interface: the model, the turn record, the checkpoint, the job store, the audit stream and the approval decision. Where the ecosystem already has an interface we adopt it; where it does not we declare a `Protocol`, so an embedder's own object plugs in by having the right methods rather than by inheriting ours.

That last one is deliberately a severing rather than a split. An earlier draft moved the REST surface into its own process; that would have obliged the desktop app to start and supervise a backend, which is precisely the coupling [`client-and-daemon.md`](./client-and-daemon.md) removed. The dependency was the problem, not the process. Cut it, and the app keeps finding one daemon and starting nothing, while splitting later becomes a deployment choice available for free.

Measured, not assumed, on both platforms. The design was established on Linux against the real import graph and then confirmed on macOS, which is where the three questions that could have killed it actually live.

## What the measurements say

Every figure is from `daisy.worker.session` — the actual parked-worker import graph, 1,240 heavyweight modules — on Linux x86-64, Python 3.13.7, against proportional set size, which is the only honest accounting when pages are shared.

| Measurement | Result |
|---|---|
| Parked worker, resident | **263.8 MB**, 5.1 s import warm-cache (10.5 s cold), 3,443 modules |
| Threads in a parked worker | **One on Linux** — LangChain, LiteLLM, httpx, tiktoken, SQLAlchemy, a2a and tree-sitter start no background threads. **Three on macOS**, and that difference is the whole story below |
| Forked child, immediately | 1.5 MB private |
| Forked child, after building a real `AgentRuntime` | 12.1 MB private, 25 tools |
| Forked child, after heavy churn and repeated full collections | **110.3 MB** private without mitigation; **13.6 MB** with `gc.freeze()` |
| Time from request to a session serving on its own socket | **54–125 ms**, median ≈ 60 ms (12 sessions × 3 repetitions, identical every run) |
| Twelve-session fan-out | **406 MB** by fork vs **3,433 MB** modelled by spawn — **88.2 %** |
| Marginal cost of one more session | **264 MB → ≈ 12 MB**, and **≈ 5 s → ≈ 60 ms** |

Fork was verified against the hazards that actually apply, not the folklore ones. A child that makes its own event loop, calls `setsid`, binds a unix socket, spawns a subprocess and logs: all pass. A child that reuses the parent's `ThreadPoolExecutor`, or touches a lock another thread held at fork time: both fail, exactly as they should. Those two failures are the entire risk.

On Linux the import graph creates neither, and the only thread in the process is the one `worker/__main__.py:37` starts itself. **On macOS that was not true, and the difference is not portable trivia — it is the invariant.** The section below is what it took to find that out, and it is the reason item 8 exists.

Three conditions follow, and all three are satisfiable:

1. **The forking parent must be single-threaded.** CPython 3.13 emits a `DeprecationWarning` on `fork()` from a multi-threaded process, and it is right to. Forking from a single-threaded parent produced no warning. Today's park mechanism — `run_in_executor(None, sys.stdin.readline)` — makes the parent multi-threaded for no reason; a blocking `accept()` needs no thread and no loop.
2. **`gc.freeze()` before forking is mandatory, not an optimisation.** Without it the first cyclic collection walks every tracked object, dirties its page, and un-shares 98 MB. With it, the decay does not happen at all.
3. **The child must take its own signals and lead its own process session.** `runtime/tools/registry.py:855` installs process-wide `SIGTERM`/`SIGHUP` handlers *at import time* that call `sys.exit(1)`. Today that is masked because the worker's entry point overrides it; it is a landmine either way, and it is deleted here.

The daemon must never import the runtime, so **the daemon cannot be the forking parent.** That is what forces a third process, and one consequence with it: the daemon can no longer `waitpid` a worker it did not fork, so worker deaths are reported rather than waited on.

### On macOS, where it counts

The three questions Linux could not answer now have answers, measured on Apple Silicon under Python 3.13.12.

| Question | Answer |
|---|---|
| May a child initialise CoreFoundation **after** the fork? | **Yes.** It initialises fresh, does not abort, and the child enumerated 26 on-screen windows through Quartz |
| Does the TCC Accessibility grant follow a fork? | **Yes.** Parent and child both report `AXIsProcessTrusted=True` |
| Does `sandbox-exec` still work from a forked child? | **Yes.** `rc=0`, `stdout='sandboxed-ok'` |
| What does it cost? | **271 MB** parked phys_footprint, **≈ 14 MB** marginal per forked session — close to the ≈ 12 MB measured on Linux |

Getting there required finding an invariant break that no self-check caught, and the way it hid is worth recording, because it is how this will break again.

**`src/daisy/base/models.py:186` runs `_catalog()` at module level**, which performs a blocking `httpx.get("https://models.dev/api.json", timeout=5)` **at import time**. On macOS that fetch spawns two persistent native network threads. Importing `httpx` alone stays at one thread; the fetch takes it to three, and they never go away. The prototype was therefore never single-threaded, condition 1 was violated, and the forked child died with:

```
objc[...]: +[__NSPlaceholderSet initialize] may have been in progress in another thread
when fork() was called. We cannot safely call it or ignore it in the fork() child process.
Crashing instead.
```

That is the **multi-threaded-fork** ObjC abort. It is not `__THE_PROCESS_HAS_FORKED_AND_YOU_CANNOT_USE_THIS_COREFOUNDATION_FUNCTIONALITY___YOU_MUST_EXEC__`, which is the abort that would have killed the design — and the two are easy to read as the same verdict. Suppressing only that one fetch takes the parent to a true one thread and every test passes; reintroducing the fetch *after* import, so the module state is byte-for-byte identical and only the threads differ, makes them fail again. A clean A/B, and the cause.

Both of the probe's own guards were incapable of firing, and both are now fixed:

| Guard | Why it was blind | What replaces it |
|---|---|---|
| `threading.enumerate()` reported one thread | It only sees threads CPython created. Native threads spawned by a library are invisible to it — CPython's own `DeprecationWarning` was right and the probe's report was wrong | mach `task_threads`, which counts what `fork(2)` cares about |
| A `sys.modules` census for CoreFoundation reported none | CoreFoundation, Foundation, CoreGraphics and SystemConfiguration are linked into the bare interpreter before any Daisy import, so a name census can never fire. Linkage turns out to be harmless — only *initialisation* matters | A blocking check on the PyObjC **bridge** modules (which is what `daisy.computer` would drag in), plus an informational dyld image listing |

The probe now also attributes any thread-count change to the exact module import that caused it, so the next violation names itself instead of presenting as a CoreFoundation verdict.

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
| **Workspace state** | `workspace.db`: projects, locations, terminals, model history, drafts, user messages | Multi-writer-safe under the `fcntl` locks that already exist. It has no supervision relationship to anything, and it is what the browser talks to |

One database file forced one writer on both records. Two files put the boundary where the reason is.

The boundaries are about *ownership*, not about process count. Supervision must be a process of its own, because that is what residency means. The other two need only be severed from it — which is why the browser surface stops depending on the daemon without moving out of it.

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

Item 8 comes first and is not optional: it is the change that makes the invariant true, and without it every other item here is built on a parent that aborts its children.

| # | Change | Where | Why |
|---|---|---|---|
| 8 | **Make `_catalog()` lazy** behind `list_models()`; nothing fetches at import | `base/models.py:186-187` | It performs a blocking `httpx.get` to models.dev **at module import**, which spawns two persistent native threads on macOS and breaks the single-threaded invariant the whole design rests on. It is worth doing on its own terms too: it costs ≈ 1 s of startup (2.85 s → 1.94 s), makes importing a module depend on a reachable third-party host, and silently yields an empty model catalogue when offline, because the `except` branch only logs |
| 9 | Add the `prototype` entry point: import the runtime, `gc.collect()`, `gc.freeze()`, park on a blocking `accept()` — no event loop, no threads | new `daemon/prototype.py`, `src/daisy/__main__.py` | Condition 1. A blocking accept keeps the parent single-threaded, which is what makes the fork legal The prototype also **asserts `task_threads() == 1` before it forks** and refuses rather than forking unsafely: the invariant is one import away from being broken again, and the failure it produces reads like a different problem entirely. That check must use mach, never `threading.enumerate()` |
| 10 | The forked child `setsid`s, restores `SIG_DFL`, binds its socket, then reports ready on a pipe | `worker/__main__.py` | `setsid` is what `peer_identity.session_for_process` depends on; verified 12/12 leaders. Ready-after-bind is what stops a `send` racing the bind, exactly as `lifecycle._await_ready` does today |
| 11 | **Delete `daemon/pool.py`** — floor, ceiling, top-up, spawn-on-empty, `worker_command`, `release` | `daemon/pool.py`, `base/configuration.py` (`daemon.warm_floor`, `daemon.warm_ceiling`) | At 60 ms there is nothing left to pre-warm. The two configuration keys go with it |
| 12 | The prototype reaps its own children and reports each exit to the daemon; `lifecycle._supervise` consumes reports instead of `await process.wait()` | `daemon/lifecycle.py:150`, `daemon/prototype.py` | The daemon cannot `waitpid` a process it did not fork. `pidfd_open`/`kqueue` would also work; reporting needs no platform-specific syscall and is about a hundred lines |
| 13 | The daemon supervises the prototype and restarts it if it dies; a dead prototype does not affect live sessions | `daemon/lifecycle.py` | Sessions are independent processes and are reparented. Only *new* sessions block, and only until the restart finishes |
| 14 | The prototype is a re-exec of the same signed image, as the worker is today | `src/daisy/__main__.py` | The macOS Accessibility grant follows code identity. A fork inherits the parent's signature, so the fleet stays one TCC row — the property `pool.py:60` exists to protect |
| 15 | Correct the documentation: nothing forks today (`pool.py:146` is `create_subprocess_exec`) | `architecture.md:51`, `documentation/README.md:40`, `development.md:53`, `worker/__main__.py:13`, `daemon/__main__.py:9`, `base/browser_assets.py:6`, `rest/services/filesystem.py:401` | Six documents and three docstrings describe a mechanism that has never existed. The `computer`-is-lazily-imported invariant is real and load-bearing — it just protects a fork that was never taken |
| 16 | `daemon.status` reports the **prototype** instead of the pool: alive, process id, native thread count, frozen-object count | `daemon/api.py:456-474`, `daisy daemon status`, `web/src/lib/api.ts:1369` | Deleting `pool.py` leaves `state.pool.warm_count`/`assigned_count` dangling in the one call every client uses for health. Replacing it rather than removing it is also what makes the `gc.freeze()` hazard detectable — the register below already claims the frozen count is visible there, and nothing was making it so |
| 17 | `base/models.py` exposes the catalogue through a function; `MODELS` stops being a module-level list | `base/models.py:186-224`, `rest/routes/settings.py:12,100,107,109` | Item 8's other half. `settings.py` does `from daisy.base.models import MODELS`, binding the list *object* at import — laziness inside `models.py` cannot help a caller already holding the old empty list. `find_model`, `available_models` and `list_models` all read it, and `runtime/runtime.py:111,892`, `daemon/services/agents.py:55` and `protocol/parts.py:137` all read those |
| 18 | Fix the module references that outlive `pool.py` | `daemon/peer_identity.py:13`, `daemon/api.py:519`, `packaging/entry.py` | `peer_identity` cites `daisy.daemon.pool` as the source of `start_new_session=True` — the sentence explaining why kernel attribution works at all. `_daemon_argv` says it mirrors `pool.worker_command`. `entry.py` says the image serves *three* roles, and it will serve four. Each is a dangling pointer inside a file whose job is to explain an invariant |

## Part III — A session is a record, not a process

| # | Change | Where | Why |
|---|---|---|---|
| 19 | Collapse the two `SessionRecord`s into one durable table | `daemon/registry.py:35` and `persistence/database.py:20` | Two classes with the same name, overlapping fields, neither complete. `daisy ps` reads the volatile one; the browser lists from the durable one |
| 20 | Session tokens are **derived**, not stored: `HKDF(master_key, session_id)` | `daemon/registry.py:128` | A durable registry must hand a token to a *new* worker on wake, which means it must be recomputable. Deriving stores nothing at rest. The precedent is `FileUrlSigner(load_or_create_secret(...))` in `protocol/files.py` |
| 21 | `status` splits: a durable `lifecycle` (`live`/`ended`) and a derived `activity` (`working`/`idle`/`asleep`) | `registry.py:26-31`, `api.py:113` | The registry already cannot answer "is it working" — `_public()` merges `busy` from `_running_contexts` for exactly this reason. Make the split explicit rather than patched |
| 22 | `relay_to_session` becomes `wake_then_relay`: no worker, ask the prototype, replay the assignment, then relay | `daemon/state.py:172` | **This is the whole mechanism, and it is one function.** Every command already funnels through it — `session.send`, `respond`, `compact`, `jobs.*`, `turn.cancel`. Reads (`get`, `list`, `tree`, `history`, `attach`) already bypass it and must keep doing so |
| 23 | Sleep an idle worker **immediately**: no turn in flight, no pending background jobs, no delay | `daemon/lifecycle.py`, reading `state._running_contexts` | **A session parked on a permission prompt is the clearest case.** `input-required` is already a durable suspension (`worker/session.py:628`) — its entire state is on disk and it holds a whole interpreter to wait. There is deliberately no linger window: at 60 ms a window would be caching against a cost that no longer exists, and a window is a tuning knob nobody can set correctly. Background jobs are the one real blocker, because they are in-process `asyncio.Task`s |
| 24 | Sessions survive a daemon restart | `daemon/api.py:477` | `daemon.restart` exists solely for the macOS TCC cache and currently documents "Every live session ends". With a durable registry it stops being true, and the confirmation dialog goes with it |
| 25 | The isolation invariant is restated, not weakened: **one worker, one session, one activation** | `daemon/prototype.py` docstring | Workers are never recycled. A slept session's next worker is a fresh copy of the prototype. Nothing in `xeac-migration.md:31` changes |
| 26 | The client's session shape follows the status split: `status` + `busy` become `lifecycle` + `activity` | `web/src/components/sessions-sidebar.tsx:44-47`, `web/src/lib/api.ts` | #21 splits the field on the server. The sidebar types both today and draws its status dot from them, so leaving the client alone renders `undefined` for every session — precisely the half-migrated state this plan exists to avoid |

## Part IV — The browser surface stops depending on the daemon

| # | Change | Where | Why |
|---|---|---|---|
| 27 | Split the database. `history.db`: turn head, history, artifacts, checkpoint, session state, registry. `workspace.db`: projects, locations, terminal states, model history, user messages, drafts | `base/paths.py`, `persistence/database.py`, `persistence/turn_store.py` | The sole-writer invariant is real for one and imaginary for the other. `base/background_store.py` already proves a worker can own a second database under the existing `fcntl` lock |
| 28 | **`rest/` becomes process-agnostic**: it depends only on `workspace.db` and the control plane's **HTTP** API, never on `daemon` internals. The daemon still mounts it; `daisy web` may serve it in-process instead | `rest/`, `daemon/__main__.py:262`, `cli/commands/web.py` | The dependency is the problem, not the process. `rest/` currently reaches through `daemon/services/*`, which use `state.session_factory` 78 times. Severing that gets the layering, and it leaves the desktop app exactly where it is — one address, discovered as now, starting nothing. **Splitting into a separate process then becomes a deployment choice rather than an architectural one**, and can be taken later for free. Taking it *now* would mean the app has to start and supervise a backend, which is the coupling `client-and-daemon.md` removed |
| 29 | The daemon keeps a **supervision core** — registry, lifecycle, prototype, `peer_identity`, `ingest`, `api`, `state`, ≈ 2,000 lines — and everything else in `daemon/` becomes a service the browser surface reaches through the API rather than by import | `daemon/` ≈ 6,996 → ≈ 5,400 after Part V and the pool deletion | The size is not the point and claiming otherwise would be dishonest: the daemon still hosts the browser surface, so it still holds the projects, settings, agents and terminal services. What changes is that nothing above it reaches *into* it, so the supervision core can be read, reasoned about, and one day moved without touching the rest. `rest` being mounted is composition, not dependency — the `__main__.py` exemption already covers exactly that |
| 30 | **Delete the entire artifact surface**, backend and frontend — see Part V | everywhere | It is a feature Daisy grew on top of the harness, half of it has never run, and it reaches into the turn loop, the tool registry, the protocol, the daemon, the REST surface and eleven frontend files to do it |
| 31 | **Rewire file leases**: the manager moves to `base/` as a process-free façade over the `fcntl` locks it already uses | `base/file_leases.py:186`, `worker/session.py:129` | `_file_lease_manager` is `None` in every worker, so `_acquire_filesystem_lease` returns `""` and **no session has ever taken a lease**. `_lock_os_key` is already cross-process `flock`; the manager object was only ever the in-process fast path |
| 32 | **Rewire location resolution**: locations travel in the assignment, resolved at `session.create` | `daemon/api.py:196`, `worker/session.py:132` | `_resolve_locations` is `None`, so every runtime synthesises one local location and multi-location projects are dead at the session level. Same treatment the sandbox and the workspace already get — resolved once, carried with the session (`confinement.md:48`) |
| 33 | Move the ChatGPT **subscription catalogue** — `fetch_subscription_models`, `get_usage_snapshot`, and the two cache-clearing calls — out of `runtime/models/codex.py` into `base/`, beside the token store that already lives there. `ChatCodexModel` keeps only the chat model | `runtime/models/codex.py`, `base/models.py`, `base/credentials.py`, `rest/routes/settings.py:36` | These four are catalogue and cache concerns, not model-call concerns. They are the *only* reason anything above the runtime imports it, and `base/credentials.py` already owns the ChatGPT tokens |
| 34 | The layering table changes: `rest` may import only `base`, `protocol`, `locations` and `computer`. Both exemptions go — `daemon` (it now uses the HTTP API, #28) and `runtime` (nothing needs it after #33) | `scripts/check_layers.py:44-49` | `rest` sitting above everything and reaching down into two layers is what let the GUI surface grow inside the process that supervises agents. The checker is where that stops being a habit |
| 35 | `sqlite_lock` learns about a third database | `base/sqlite_lock.py:38-66` | It knows exactly two, `history.db` and `background.db`, each with its own path, thread lock and accessors — and its docstring explains at length why they must not share one. `workspace.db` needs the same treatment, and a worker and the browser surface writing it through the history lock would reintroduce the loop deadlock that docstring exists to prevent |

## Part V — The artifact surface is deleted

Not trimmed to the dead half. Deleted. The version history has never run in this architecture — `state.capture_queue` is never assigned and `_capture_worker` is never started, so `_enqueue_capture` returns at `artifacts.py:75` on every call — and the half that does run, `open_artifact` and its preview panel, is machinery layered on top of the harness rather than part of it. A tool that renders a file into a tab, a rewriting HTTP proxy so that tab can load external pages, a websocket relay for the same, an injected JavaScript runtime, an image-annotation round trip back into the model's context, and four database tables, are a product feature wearing a harness's clothes. It is the single largest thing in the tree that nothing else depends on.

The A2A `Task.artifacts` type stays, and it is not the same thing: it is the protocol's own name for a turn's deliverable, produced by `updater.add_artifact` (`worker/turn.py:587`) and stored in `turn_artifacts`. Deleting that would delete a turn's result and break A2A compliance.

### Deleted whole

| File | Lines | What it was |
|---|---|---|
| `daemon/persistence/artifacts.py` | 640 | Shadow-git capture, file index, surfaces, annotations, prune, the `@ctx=` path codec |
| `daemon/persistence/versioning.py` | 438 | The shadow-git version store |
| `rest/services/proxy.py` | 272 | The rewriting pass-through proxy, for `open_artifact` of an external URL |
| `rest/routes/artifacts.py` | 259 | All eleven routes — index, versions, diff, restore, bytes, annotations ×3, page, proxy, proxy websocket |
| `runtime/annotation_stamping.py` | 245 | Numbered badges stamped onto annotated images so a vision model could read them back |
| `base/assets/proxy_runtime.js` | 149 | Injected into proxied pages |
| `base/assets/artifact_runtime.html` | 79 | Injected into rendered artifacts |
| `base/browser_assets.py` | 32 | The loader for both |
| `runtime/prompts/artifact_render_error.md`, `artifact_not_previewable.md` | 6 | Model-facing copy for failures that can no longer occur |
| `web/src/components/artifact-history.tsx` | 183 | The filmstrip |
| `web/src/lib/artifact-annotations.ts` | 115 | Client-side annotation state |
| `web/src/components/native-webview.tsx` | 104 | The Tauri webview host for a preview |
| `web/src/lib/native-artifact.ts` | 52 | Native preview bridge |
| `web/src/components/artifact-bridge.tsx` | 34 | The web preview bridge |
| **Total** | **≈ 2,608** | |

### Excised from

| Where | What comes out |
|---|---|
| `daemon/persistence/database.py:119-216` | `ArtifactVersionRecord`, `ArtifactFileRecord`, `ArtifactSurfaceRecord`, `ArtifactAnnotationRecord` |
| `daemon/state.py:117` | `capture_queue` |
| `runtime/runtime.py:36,197,325,462-470,762-798` | The tool import and roster entry, the `_tool_open_artifact` dispatch entry, `_artifact_capture`, `set_artifact_capture`, `_capture_written_artifacts`, `_artifact_surface_id` |
| `runtime/tools/dispatch.py:1154-1230` and four capture call sites at `:609,881,1173,1215` | `_tool_open_artifact` whole, and every `_capture_written_artifacts` call after edit/write/bash |
| `runtime/tools/registry.py:474-580` | `open_artifact`, `artifact_kind_for`, `build_open_artifact_result`, `_ARTIFACT_IMAGE_SUFFIXES` |
| `worker/session.py:133,509-510` | `_capture_artifacts` and its injection |
| `worker/turn.py:51` and its call sites | `annotation_image_blocks`, `normalize_annotation_payloads` |
| `protocol/parts.py:31,46`, `protocol/metadata.py` | `ARTIFACT_EVENT_KIND` and its inbound branch |
| `protocol/dtos.py` | `ArtifactAnnotationSaveRequest`, `ArtifactRestoreRequest` |
| `rest/routes/sessions.py:14`, `rest/routes/projects.py:19` | The `_prune_session_artifacts` calls on session and project deletion |
| `base/tuning.py:258` | The annotated-screenshot tunable |
| `web/src/components/chat-panel.tsx` | The artifact panel, its tab strip, and the preview pane — 242 references in one file, the largest single piece of surgery in this plan |
| `web/src/components/tool-views/index.tsx` | 165 references: the `open_artifact` call and result views |
| `web/src/lib/use-chat.ts` | 110 references: artifact events in the reducer |
| `web/src/lib/api.ts` | 52 references: every artifact call |
| `web/src/components/attachment-chips.tsx`, `chat-message.tsx`, `chat-input.tsx`, `tool-call.tsx`, `tool-group.tsx`, `panel-tiles.tsx`, `ui/panel.tsx`, `ui/panel-tab.tsx`, `ui/segmented-toggle.tsx`, `background-jobs-panel.tsx` | 77 references between them |
| `web/src/lib/tool-display.ts` | The `open_artifact` label and icon |
| `web/messages/en.json`, `ja.json` | The artifact keys, plus every key the deletion orphans. **The 18/16 asymmetry an earlier draft recorded here was a miscount** — it counted grep hits, not keys; the catalogues are exact mirrors, and `xeac-migration.md:396` was right. The real finding is twenty-nine orphaned keys, and two more that were already orphaned, which is what `scripts/check_translations.py` now exists to prevent |

**No wire event goes with this**, so no schema regeneration is implied. `protocol/events.py` defines no artifact event; `ARTIFACT_EVENT_KIND` (`protocol/metadata.py:21`) is an inbound *part kind*, and the tool result rides the ordinary `_tool_result_part` path. `bun run check:events` should still be run to prove that.

## Part VI — Configuration and documentation

Deleting a feature that was documented is not finished when the code is gone. These are the surfaces that describe things this plan removes, and each of them lies the moment its subject does.

| # | Change | Where | Why |
|---|---|---|---|
| 36 | Delete `daemon.warm_floor` and `daemon.warm_ceiling` from the schema and the shipped template | `base/configuration.py:328,335`, `base/configuration.yaml:10-12` | The pool goes in #11. **The template uses `daemon.warm_floor` as its worked example** for `daisy configure` in the header comment, so deleting the key silently breaks the one thing that teaches the command |
| 37 | Remove the warm-pool paragraph and both keys from the guides | `documentation/architecture.md:57`, `documentation/configuration.md:194-195` | `architecture.md` describes the floor, the ceiling and the spawn-on-empty fallback in a paragraph that becomes wholly false |
| 38 | Add the prototype to the architecture diagram and prose; correct the request lifecycle | `documentation/architecture.md:5-37,100-107` | The diagram shows a daemon that assigns a warm worker. It will show a daemon that asks a prototype to fork one, and a session that may have no process until it is messaged |
| 39 | Remove `open_artifact` and the artifact panel from the guides and the skill | `documentation/tools.md`, `development.md`, `installation.md`, `configuration.md`, `architecture.md`, **`.agents/skills/harness-configuration/SKILL.md` (8 mentions)** | The skill is loaded by agents working on the harness, so a stale one teaches a tool that no longer exists — worse than a stale document a person reads |
| 40 | Correct the daemon-restart warning now that sessions survive it | `documentation/cli.md`, `documentation/architecture.md:98` | `daisy daemon restart` currently documents ending every live session, which #24 makes untrue |
| 41 | Document the library entry point | `documentation/README.md`, a new guide | #6 adds a supported way to embed the harness. An entry point nobody documents is an entry point nobody uses |
| 42 | Correct the warm-worker sentence in the README | `README.md:17` | It tells a reader that `daisyd` "parks a couple of warm workers so spawning a session is usually a socket write". After #11 there is no pool, and this is the first description of the daemon anyone reads |
| 43 | Correct "three entry points" everywhere it is asserted | `documentation/development.md:3,34,72`, `packaging/entry.py`, `.agents/memories/desktop-build.md:24` | The prototype makes four. In `desktop-build.md` the claim is load-bearing: it is the premise of the code-identity argument |
| 44 | Rewrite the agent-facing memories | `.agents/memories/harness-layout.md`, `.agents/memories/desktop-build.md:36` | `harness-layout.md` describes three entry points, a worker pool, and the invariant as *"a pre-warmed worker must not have loaded PyObjC"* — the right rule stated for the wrong mechanism. `desktop-build.md:36` says `daemon.restart` **ends live sessions**, which #24 makes false. Agents working on this repository read these, so a stale one is not a stale document, it is wrong instructions |
| 45 | Restate the fork invariant in the development guide | `documentation/development.md:53` | It reads "a parked worker that has loaded PyObjC is not safe to fork", describing a fork that has never happened. It becomes a statement about the prototype, and gains the half that actually bit: single-threaded, measured natively |

## Part VII — The library's seams are interfaces

Part I made the runtime re-entrant and #6 gave it a front door. That is enough to *run* the harness in your process and not enough to *embed* it in your program, because everything the harness writes down still goes exactly one place, chosen by us, at a path the caller cannot name.

The evidence, measured rather than assumed. A `Session` constructed and its runtime built touch nothing on disk — good. Then the first backgrounded command creates `background.db`, `-wal`, `-shm` and `.lock` under the caller's XDG data directory, through `get_background_job_store()`, a module-level singleton reading a fixed path. A script that runs one bash command in the background leaves a SQLite database behind, and there is no argument that prevents it.

The same shape repeats. The chat model is `build_chat_model(...)` from configuration at `runtime.py:451`, so an embedder who already has a `BaseChatModel` — configured, instrumented, rate-limited, mocked in their tests — cannot use it. A gated tool call can only be answered by consuming `Suspended` and calling `resume()`, so `ask()` raises rather than asking. And the conversation checkpoint lives on the daemon's SQLite task store, so a library session cannot resume at all.

### The rule

**A seam is an interface, not a class of ours.** Where an interface already exists in the ecosystem we adopt it and make it injectable; where none does we declare a `typing.Protocol` (PEP 544) and ship at most one obvious default behind it. `Protocol` is the point: it is *structural*, so an embedder's existing object satisfies it by having the right methods — no base class to inherit, no registry to join, no import of Daisy in their type. That is what "plug and play for any approach anyone else might use" means concretely, and it is why this is deliberately **not** a set of `MemoryX`/`FileX`/`SqliteX` classes. Those would be a taxonomy of our opinions where the deliverable is the shape of the hole.

Two of these seams are already right, and they are the pattern the rest follow: `SessionAccess` (`runtime/tools/sessions.py:71`) and `LocationExecutor` are `Protocol`s, injected, with the daemon supplying the real one. Nothing new is being invented here — the pattern is being finished.

No dependency is added. `typing.Protocol` is standard library, and the two adopted interfaces come from packages already required.

### The seams

| Seam | Today | Becomes | Default when unset |
|---|---|---|---|
| **Model** | `build_chat_model()` from configuration only (`runtime.py:451`) | Accept a LangChain **`BaseChatModel`** directly. **Adopted, not invented** — it is already the interface every provider in this tree implements | Built from configuration, as now |
| **Turn record** | `AppendOnlyTaskStore(engine)` over a fixed `history.db` | Accept an a2a **`TaskStore`**. **Adopted** — a2a already declares the ABC and Daisy already implements it; only the wiring is hard-coded | The daemon's append-only SQLite store |
| **Checkpoints** | `save_turn_state`/`load_checkpoint`/`load_session_state` bolted onto the task store | New `Checkpoints` Protocol: `save(session_id, state)` / `load(session_id)`. This is what makes a library session resumable at all | In memory |
| **Background jobs** | `get_background_job_store()` — module singleton, fixed SQLite path, created on first job | New `JobStore` Protocol, narrowed to what `runtime/background.py` actually calls | In memory — **the library stops writing to the caller's disk** |
| **Observation** | `on_record_event` / `on_record_message` — two ad-hoc callbacks **with no supplier anywhere** | One `Observer` Protocol taking a frozen `Observation(session_id, kind, at, data)`. Returns `Awaitable[None] | None`, the tolerance `MCPEventCallback` already uses in this tree | None — the events are dropped, as they are today |
| **Approvals** | `Suspended` + `resume()` only; `Session.ask()` raises | New `Approvals` Protocol: `decide(request) -> Approval`, shaped from the existing `SuspensionGate` rather than invented | None — yield `Suspended`, exactly today's behaviour |
| **Peers** | `SessionAccess` Protocol, injected | Unchanged. Renamed to `peers=` on the public surface | None — the composition tools are absent |
| **Execution** | `LocationExecutor` Protocol, injected | Unchanged | Local execution |

| # | Change | Where | Why |
|---|---|---|---|
| 46 | Declare the four new Protocols in `base/ports.py`, re-exported from `daisy/__init__.py`; every one `@runtime_checkable` | new `base/ports.py` | `base` is the lowest layer, so `runtime`, `worker` and `daemon` may all depend on them and the layer table needs no exemption. Re-exporting means an embedder writes `from daisy import Approvals` and never learns the internal layout. `runtime_checkable` buys one thing: a constructor that says *which method is missing* instead of failing at the first call |
| 47 | `AgentRuntime` accepts `model`, `checkpoints`, `jobs`, `observer`, `approvals`; `build_chat_model` becomes the fallback rather than the only path | `runtime/runtime.py:395-451` | The constructor already takes `session_access`, `mcp_manager`, `sandbox`, `locations` and `file_lease_manager` by injection. These are the five that were left as globals, singletons or dead callbacks |
| 48 | **Delete `on_record_event` and `on_record_message`.** `_record_event`/`_record_message` route to the `Observer` | `runtime/runtime.py:399-400,425-426,864-872` | A **fifth** permanently-`None` injection point, alongside the four in `SessionExecutor`. `bash_auto_approved`, `mcp_auto_approved`, `screen_auto_approved` and `goal_updated` are an audit trail that has never had a reader — the fix is to give it a named home, not to keep two callbacks nobody wires |
| 49 | `runtime/background.py` takes its store by argument; `get_background_job_store()` and its module singleton go | `runtime/background.py:42`, `base/background_store.py` | This is the one that writes to the caller's disk unasked. The existing SQLite implementation survives as what the worker passes in |
| 50 | Split the checkpoint methods off the task store into a `Checkpoints` implementation the daemon supplies | `daemon/persistence/turn_store.py:400-497` | `save_turn_state`, `load_checkpoint` and `load_session_state` are on `AppendOnlyTaskStore` because they share a database, not because they are A2A. Off the class, the A2A store is exactly a2a's `TaskStore` and can be swapped for one |
| 51 | `Approvals` is consulted before a gate suspends; absent, the turn suspends as now | `runtime/turnloop.py:589`, `runtime/permissions.py` | The suspend/resume dance is right for a client with a human on the other end and wrong for a script. `Session.ask()` stops raising when approvals are supplied |
| 52 | `Session` takes every port as a keyword argument; `session_access` is renamed `peers` | `src/daisy/__init__.py` | Individual keywords rather than a bag, as `httpx.Client(transport=…, auth=…)` does: each is typed, discoverable and independently defaulted. The rename is the public name matching what the thing is |
| 53 | The layering checker learns that `base/ports.py` may be imported by every layer, and that nothing may import a *concrete* store where a port exists | `scripts/check_layers.py` | Items 46–52 are a class, exactly as 1–5 were. Without a rule the next fixed path arrives the same way this one did |
| 54 | Document the ports with a worked example per seam | new `documentation/library.md` (#41) | An interface nobody can find is a class nobody can replace. The guide is where "bring your own model / store / approver" stops being folklore |
| 55 | `GlobalConfiguration.load(seed=False)`, and the library uses it | `base/configuration.py:765`, `src/daisy/__init__.py` | Found by measuring rather than reasoning: with every port in place the library still left one file behind, because `load()` seeds the configuration template on first run. Right for a person who has just installed Daisy and wrong for a program that imported us |
| 56 | Move `reap_orphaned_processes` to `base/background_store.py` and give it a caller | `runtime/background.py`, `daemon/__main__.py` | It kills process groups a SIGKILL'd harness left holding ports, and **nothing has ever called it** — so the orphans it describes have been accumulating. Its caller has to start before any session and must not import the runtime, which is the daemon exactly |

### What this is deliberately not

It is not a persistence framework. There is no registry, no entry-point discovery, no `daisy.plugins` namespace, no configuration key naming an implementation by dotted path. You pass an object; it either has the methods or it does not. Anything more would be machinery in front of a constructor argument.

It is not a taxonomy of implementations. One default per port, where a default is needed at all — in-memory for `Checkpoints` and `JobStore`, nothing for `Observer` and `Approvals`. The SQLite implementations already exist and stay where they are, supplied by the daemon, which is the layer that has a database.

And it does not abstract what is already a value. The confinement `Profile` is a frozen dataclass the caller constructs and hands over; `GlobalConfiguration` is a pydantic model `Session` already accepts. Neither needs an interface, and giving them one would be the forest this section exists to avoid.

## Part VIII — Where the prompt comes from is a seam too

Part VII made everything the harness *writes* replaceable. This is the other half: everything it *reads*.

A library session assembles its prompt from five kinds of material, and every one is found by walking hardcoded filesystem paths. Constructing a `Session` in a program means that program silently acquires the contents of `~/.agents`, and — this is the one that should not survive a reading — of two other vendors' configuration files:

```python
_GLOBAL_INSTRUCTION_PATHS = (
    Path.home() / ".config" / "opencode" / "AGENTS.md",
    Path.home() / ".claude" / "CLAUDE.md",
    Path.home() / ".agents" / "AGENTS.md",
    Path.home() / "AGENTS.md",
)
```

That is a library reading opencode's and Claude Code's instruction files out of a user's home directory because it was imported. It is right for `daisyd`, where the person running it *is* the person those files belong to. It is indefensible for an embedded harness, and no argument can turn it off.

### The sweep

| Material | Found how | Reloaded | Hardcoded |
|---|---|---|---|
| **Agent profiles** | `agent_directories_for()` — four roots | At session build | `BUNDLED_DOTAGENTS_ROOT/agents`, `~/.agents/agents`, `<cwd>/.agents/agents`, `<cwd>/agents` |
| **Skills** | `skill_directories_for()` — four roots | **Every turn** (live reload) | same four, `skills` |
| **Memories** | `memory_directories_for()` — two roots | Every turn | `~/.agents/memories`, `<project>/.agents/memories` |
| **Instructions** | `load_instructions(working_directory)` | Every turn | Four `Path.home()` literals, **two belonging to other products**, plus a per-ancestor walk |
| **Prompt templates** | `PromptLoader(runtime/prompts)` | Per render | The package directory; the system prompt cannot be replaced at all |
| **MCP servers** | `mcp.json` under the `.agents` roots | On change | Values already, but discovered from disk |
| **Remote agents** | `remote-agents.json`, same roots | On change | Same |
| **Configuration** | `configuration.yaml` under XDG | Once | Already injectable; #55 stopped it seeding |
| **Credentials** | `auth_file_path()` | Per call | Reached only when we build the model, so `model=` already bypasses it |

### One port, not six

The first five are the same thing: **named text in a namespace**. An agent, a skill, a memory, an instruction file and a prompt template differ in how they are *parsed*, not in how they are *found*. Six ports for one concept is the forest; one port with five accessors is the interface.

The precedent is Jinja2's `Loader` — `FileSystemLoader`, `PackageLoader`, `DictLoader`, `FunctionLoader` behind one `get_source`, which is exactly this shape and is the most-copied loader design in Python. Parsing stays ours: a `Skill` is still a `Skill`, so an implementation supplies material and never has to know our formats.

| # | Change | Where | Why |
|---|---|---|---|
| 57 | Declare `Catalogue` in `base/ports.py`: `agent(name)`, `skills()`, `memories()`, `instructions()`, `prompt(name, variables)` | `base/ports.py` | One interface over the five, because they are one concept. `skills()` and `memories()` are called per turn, so an implementation controls live reload by being lazy or not — the harness stops deciding that for it |
| 58 | `FileCatalogue` is today's behaviour, unchanged, and becomes the default the CLI and the daemon pass | new `base/catalogue.py` | The path-walking logic is *correct* for a person's machine. It moves rather than dies; what changes is that it is a choice |
| 59 | `Session(catalogue=...)`, and `agent=` accepts an `AgentConfiguration` as well as a name | `src/daisy/__init__.py` | The direct answer to "build a session entirely in code". Accepting the value beats looking it up, and costs no interface at all |
| 60 | **The library's default catalogue reads no home directory.** `FileCatalogue` gets an explicit set of roots; the library's default is the working directory only | `base/catalogue.py`, `src/daisy/__init__.py` | A program that imported us did not ask to inherit `~/.agents`, and it certainly did not ask to read `~/.claude/CLAUDE.md`. `daisyd` and the CLI keep the full set, because there the person and the home directory are the same person |
| 61 | `PromptLoader` becomes a `Catalogue` accessor; the packaged templates are its default | `base/configuration.py:1258`, `runtime/` | The system prompt is the harness's most opinionated artefact and the least replaceable thing in it. An embedder who cannot change it is running our product, not their own |
| 62 | The layering checker forbids `Path.home()` and `Path.cwd()` outside `base/catalogue.py`, `base/paths.py` and the composition roots | `scripts/check_layers.py` | This is a class, like items 1–5 and 46–52 were. `instructions.py` is what happens without a rule |
| 63 | MCP and remote-agent discovery is skipped when the caller supplies the values | `base/configuration.py:786-787`, `from_dotagents_roots` | `from_yaml` unconditionally scans the `.agents` roots for `mcp.json`, so merely *loading configuration* touches the filesystem beyond the configuration file |

## Part IX — The command surface

`serve` and `web` are the shape [opencode](https://opencode.ai) settled on, and the reason they hold up is that they split by *intent* rather than by implementation: `serve` is the API alone, `web` is the same server plus opening a browser. Daisy has the same two things and names them for neither.

| # | Change | Where | Why |
|---|---|---|---|
| 64 | `daisy daemon start` becomes **`daisy serve`**; `daemon` keeps `status`, `stop`, `restart`, `endpoint` | `cli/__main__.py` | Starting the API in the foreground is not "inspecting the daemon", which is what the rest of that noun group does. `serve` is the name three of the four surveyed tools would recognise |
| 65 | **`daisy web`** keeps its name and gains the browser-open | `cli/commands/web.py` | It already serves the interface; opening the browser is what makes it `web` rather than a second `serve`. `--no-open` for a remote box |
| 66 | `daisy open` becomes **`daisy app`** | `cli/__main__.py` | `open` names no object. Codex uses `app` for exactly this, and it reads correctly next to `serve` and `web` |
| 67 | Add **`daisy run <prompt>`** — one turn, no daemon, straight through `daisy.Session` | new `cli/commands/run.py` | The gap the survey makes obvious: every comparable tool has a one-shot mode and Daisy needs three commands to approximate one. It is a handful of lines *because* Part VII exists, and it is the best possible proof the library surface is real — the CLI becomes its first consumer |
| 68 | Add **`daisy auth`** — `login`, `logout`, `status` for the ChatGPT subscription | new `cli/commands/auth.py` | `auth` is the near-universal spelling, and today signing in is possible **only through the browser interface**: a headless install cannot reach the one provider that needs no API key |


## Verification

There is no test suite — `pyproject.toml` sets `testpaths = ["tests"]` and `tests/` contains no test files — so verification is built here rather than inherited, as it was for `xeac-migration.md`. The order matters: each stage depends on the one before it being true.

| Stage | What it proves | How |
|---|---|---|
| **Structure** | The layering held and nothing was dropped | `scripts/check_layers.py`, with the two new rules from #7 and #34. A symbol-inventory diff against the pre-change tree, where every removed public symbol appears on the deletion list |
| **The invariant** | The prototype is single-threaded and forkable | `scripts/probe_fork_macos.py`, unmodified, exit 0. **This gates Part II**, and it is the one check that must run on macOS rather than in CI |
| **Re-entrancy** | Part I actually removed the shared state | Two `AgentRuntime` instances in one process, with different sandbox profiles, each running a `bash` call — the second must not observe the first's confinement. This is the regression that `dispatch.py:565` can reintroduce |
| **A turn** | The harness still works end to end | Create a session, send a message, watch it complete over `attach`. The path most likely to be broken by Part I, because every tool client moved |
| **Sleep and wake** | Part III is real | Send a message, wait for idle, assert no worker process exists, send a second message, assert the reply arrives and the conversation continued. Then the same across a daemon restart |
| **A permission gate survives sleeping** | The best case of Part III | Drive a turn to `input-required`, assert the worker is gone, answer it, assert the turn resumes |
| **Fan-out** | The economics claimed here | Create twelve peers; assert the fleet's total footprint is closer to one prototype than to twelve workers |
| **Reaping** | Supervision still works without `waitpid` | Kill a session's process directly; assert the prototype reports it and the daemon marks the session failed. Kill the prototype; assert live sessions are unaffected and a new session still starts |
| **The wipe** | Part V took the right things | `grep -ri artifact` over `src/` and `web/src/` returns only `turn_artifacts`, `task.artifacts` and `add_artifact`. `bun run build`, `bun run check:events` and `scripts/check_translations.py` pass |
| **The seams** | Part VII made them replaceable rather than merely named | For each port, drive a turn with a caller-supplied implementation and assert it was used: a stub `BaseChatModel`, a list-appending `Observer`, an auto-allowing `Approvals`, a dict `Checkpoints` a second `Session` resumes from. Then assert the negative that motivated it — a library session that runs a background job creates **no** file under the caller's XDG directories |

These are throwaway tests in the sense `xeac-migration.md` used the term — written to prove this change, not to become a suite. A real suite is separate work with a separate goal.

## Data on disk

Zero backward compatibility, stated concretely rather than left to be discovered.

An existing `history.db` carries four artifact tables and a `sessions` row shape that #19 replaces. Nothing migrates them: the artifact tables are dropped, and the registry merge rewrites `sessions`. Existing **turns, checkpoints and session state are preserved** — they are the durable record and the whole point of the store — so a person's transcripts survive while their artifact history, which has always been empty, does not. `workspace.db` is created empty on first start and the projects, locations, terminal states and model history are copied across once, because those are cheap to move and annoying to lose.

## Deleted

| What | Where | Why it goes |
|---|---|---|
| The warm worker pool, its floor, ceiling and both configuration keys | `daemon/pool.py`, `base/configuration.py` | 60 ms leaves nothing to pre-warm |
| Eight module-global setters and their read sites | `runtime/tools/registry.py`, `file_operations.py`, `sessions.py`, `base/tuning.py` | Configuration and per-runtime state pretending to be process state |
| `on_record_event`, `on_record_message`, `get_background_job_store` and its module singleton | `runtime/runtime.py:399-400`, `base/background_store.py` | Two callbacks nobody supplies and one singleton nobody can redirect. Replaced by the `Observer` and `JobStore` ports (#48, #49) |
| `model_context`, and the MCP client's artifact extraction | `runtime/tools/file_operations.py`, `base/mcp_client.py` | `model_context` claimed to be what the model sees while sitting in the same payload as the full result; nothing has ever read it. The extraction fed the deleted panel, and its render-payload stripper would otherwise have silently discarded tool output |
| Thirty-one orphaned message keys | `web/messages/*.json` | Twenty-nine stranded by Part V, two that predate it |
| The module-level `signal.signal()` and `atexit.register()` | `runtime/tools/registry.py:849-856` | A library must not seize a process's signals |
| `_on_new_context`, `_session_permission_mode_for`, `_ensure_mcp_servers`, `_ensure_session_workspace`, `_claim_persisted_work_habits_acknowledgement` | `worker/session.py:122-131` | Genuinely superseded: the mode is fixed at create, the workspace is resolved at create, MCP is per-session |
| `services/sessions.py:_claim_work_habits_acknowledgement` and `SessionLifecycleRecord` | `daemon/services/sessions.py:21`, `persistence/database.py:56` | No caller; the worker keeps its own in-memory flag |
| The duplicate `SessionRecord` | `daemon/registry.py:35` | Merged into the durable table |
| **The entire artifact surface** — version history, `open_artifact`, the preview panel, the rewriting proxy, the injected runtimes, image annotations, four tables, eleven routes, five frontend modules | Part V | ≈ 2,608 lines deleted outright plus excisions across nineteen files. A product feature layered on the harness, half of which has never run |

## What is deliberately not changing

The daemon stays. Two use cases earn it and nothing else serves them: a session that outlives the terminal that started it, and a harness reached from another machine over a URL or an SSH tunnel. A daemonless design in the shape of Podman is genuinely attractive and fails both — and it strands `peer_identity`, because with no daemon socket there is no kernel-attested peer and the permission clamp falls back to the tokens that module was written to stop trusting.

Sessions stay processes. The XEAC migration's prize was crash isolation for a harness whose composition story is fan-out; a peer that runs out of memory must not take its parent and siblings with it. Part III removes the cost of that isolation without removing the isolation.

The confinement design is untouched. It was never the reason for process-per-session and it is not affected by any of this.

`ask_user`, `daisy send --wait`, the subtree scoping on every control-plane verb, and the permission clamp all keep their present behaviour.

The A2A `Task.artifacts` type survives the Part V wipe. It shares a word with what is being deleted and is not the same thing: it is the protocol's name for a turn's deliverable, written by `updater.add_artifact` (`worker/turn.py:587`) into `turn_artifacts`. Removing it would remove a turn's result and take A2A compliance with it.

## Invariants

`create` remains the only place a session's configuration is set, and a child is still clamped to no looser a mode than its parent. A worker still serves exactly one session and is never recycled — a slept session's next worker is a fresh copy of the prototype, not a reused one. The daemon still never imports the runtime, which is now doubly load-bearing: it is what keeps the control plane light *and* what keeps the prototype, rather than the daemon, the thing that forks. `computer/` is still never imported at module level, and that invariant stops being theoretical: it is what keeps CoreFoundation out of the prototype's address space, which is what makes forking legal on macOS at all.

Three invariants join them. Nothing under `runtime/` holds mutable module-level state, registers an `atexit` hook, or installs a signal handler. Nothing anywhere performs network or framework work at import time. And **the prototype is single-threaded at the moment it forks** — measured with mach `task_threads`, never `threading.enumerate()`, and asserted by the prototype itself rather than trusted. That last one is the invariant the whole of Part II rests on, it has already been broken once by an import-time HTTP call nobody thought of as concurrency, and when it breaks the symptom is an abort that reads like an entirely different verdict.

## Accepted costs

There are two resident processes where there was one: the daemon and the prototype. The prototype's 264 MB is paid whether or not a session is running — the same money the warm pool spends today on two parked workers, for one process instead of two, and with no ceiling on how many sessions it can serve. The daemon does not shrink as far as it might, because it keeps mounting the browser surface; what it gives up is depending on it.

Waking a slept session pays an MCP reconnect, and for a stdio server spawned through `uvx` that can be seconds — far more than the 60 ms fork. There is deliberately no linger window to hide it. A window would be a cache with a timeout nobody can set correctly, tuned against a cost that only exists because MCP connections are expensive; if the wake becomes painful, the honest fix is to make those connections cheap, not to keep an interpreter alive so the user does not notice.

**The artifact surface is gone, including the half that worked.** File-version history, the filmstrip, diff, restore and annotations regress nothing, because none of them has ever had a producer. `open_artifact` and its preview panel are a real loss: the agent can no longer render an HTML page, a chart or an image into a tab, and a person watching a session loses the one view that was not a transcript. That is taken deliberately. The feature reached from the tool registry through the turn loop, the protocol's part kinds, the daemon's persistence, a rewriting HTTP proxy and eleven frontend files, and it earned none of that reach; an agent that wants to show you something can write a file and tell you where it is. Rebuilding it later should start from the worker and the tool result, not from the daemon.

A session's capability token becomes recomputable from a master key rather than existing only in RAM. That is a real widening — anything that can read the master key can mint any session's token — bounded by the fact that the same reader could already read the daemon's own token from the same 0600 directory.

## Hazard register

| Hazard | Why it is real | Detection |
|---|---|---|
| **The single-threaded invariant breaks again** | It broke once already, silently, and the failure presented as the CoreFoundation verdict rather than as itself. Any dependency that touches the network or a framework at import time re-breaks it, and Python-level introspection will not show you | The prototype asserts `task_threads() == 1` before forking and refuses otherwise (#9). `scripts/probe_fork_macos.py` counts with mach and attributes any change to the exact module import that caused it |
| **A dependency initialises the Objective-C runtime at import** | The `computer`-is-lazy invariant covers Daisy's own code; it cannot cover a third-party package that reaches for a framework on darwin | The probe blocks on PyObjC **bridge** modules in `sys.modules`. Note what does *not* work: a name census for CoreFoundation, which is linked into the bare interpreter and therefore always present — the probe lists loaded dyld images for information only |
| **`gc.freeze()` is forgotten or reverted** | Without it the saving drops from 88 % to roughly a third of that, silently — nothing breaks, memory just grows | The prototype records its frozen count in `daemon.status`; a zero is visible |
| **Sleeping a session with background work** | A background job is an in-process `asyncio.Task`; sleeping its worker kills it | The idle test includes `has_pending_jobs()`. `background_store` already models the loss if it is ever wrong |
| **Sleeping immediately makes MCP-heavy sessions feel slow** | With no linger window, every message to an idle session respawns its stdio servers. The fork is 60 ms; a cold `uvx` is not | Measure wake latency split into fork and MCP-connect, reported in `daemon.status`. If the second dominates, the answer is connection reuse, not a window |
| **Two writers to `workspace.db`** | The worker and whatever serves `rest` are both writers | They already share `base/sqlite_lock.py`'s cross-process `flock`; `background.db` has worked this way all along |
| **`rest` drifts back into importing `daemon`** | It is the path of least resistance, and it is how the surface grew inside the daemon in the first place | The layering checker, with both exemptions removed (#34) |
| **The artifact deletion misses a tendril, or takes the wrong one** | It reaches nineteen files across five layers plus two i18n catalogues, and *artifact* also names the A2A deliverable, which stays. A missed reference is an import error; a wrong one silently removes a turn's result | `turn_artifacts`, `task.artifacts` and `updater.add_artifact` must survive untouched, and a turn must still produce a `result` artifact. Everything else matching `artifact` outside `protocol/` and the turn store should be gone — grep is the check, and the layering checker plus `bun run build` catch the rest |
| **A deletion strands message keys instead of drifting them** | The catalogues turned out to be exact mirrors, so the failure mode is not drift between languages — it is copy for surfaces that no longer exist, which nothing renders and nobody can identify later. Part V stranded twenty-nine keys | `scripts/check_translations.py`, run by `bun run build`: identical key sets across languages, and every key referenced under `web/src` |
| **A port is declared and then bypassed** | The concrete store still exists and still works; reaching for it directly is easier than threading an argument, which is exactly how `get_background_job_store()` became a singleton | The layering checker forbids importing a concrete store where a port exists (#53). The negative assertion in the verification table — no files under the caller's XDG directories — fails loudly if anything reaches past the port |
| **`Protocol` gives no compile-time guarantee** | Structural typing means a near-miss signature type-checks nowhere and fails at the first call, possibly deep in a turn | Every port is `@runtime_checkable` and validated at the constructor, so a missing method is named at `Session(...)` rather than surfacing as an `AttributeError` mid-turn |
| **A partially-migrated registry** | Two registries becoming one touches `ps`, the sidebar, the reaper and attribution together | They are merged in one change; there is no interval in which both exist |

## Left open

**MCP stdio servers still run unconfined,** unchanged from `confinement.md`, and now with a second edge: each session connects its own, so a slept and woken session respawns them. Whether an MCP server should carry its own declared profile is still the open question it was.

**Background work still dies with its worker.** Part III makes that visible rather than solving it: the honest fix is a detached job that outlives the session and reports through the daemon, which is a subsystem, not a line. It is deliberately not in this plan.

**Terminals stay in the daemon, and that is the weakest part of this plan.** `brokers/terminals.py:254` calls `pty.fork()` inside the daemon — which by then has a populated thread pool from seventy-three `asyncio.to_thread` call sites. It is safe today only because the child `execvpe`s immediately, and fork-then-exec is fine; it is nonetheless the one place the process that supervises every session forks itself. A terminal genuinely wants a resident host and belongs to no boundary here cleanly. If anything ever justifies making `rest` its own process, this is it — which is the second reason #28 severs the dependency rather than merely relocating the code.

**Test 3 measured the interpreter's identity, not Daisy's.** The Accessibility probe ran under `uv run python`, so the TCC subject was the interpreter rather than the signed bundle. Parent and child agreeing is the meaningful signal — the grant follows a fork — but the authoritative answer for Daisy's own code identity needs the frozen binary, and that is worth confirming before shipping computer control on forked sessions.
