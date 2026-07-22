---
created: 2026-07-20T06:16:56Z
updated: 2026-07-20T06:16:56Z
commit: 553e7d0
---

# Server Module Decomposition

This plan covers the second body of work the PR #5 review surfaced: not the harness core
(that is [`runtime-refactor-and-hardening.md`](./runtime-refactor-and-hardening.md)), but the
FastAPI **server** layer under `harness/server`. A close read found the routes had been
"split out" of `app.py` only cosmetically — they still imported everything back from it — and
that `app.py` had grown into a 4,162-line god-module holding the DTOs, the ORM, the shared
state, ~90 helper functions, the boot lifespan, and the ASGI wiring all at once. One symptom
of that tangle was a pair of routes that had been broken for real, crashing on every request.
The directive was to fix the pre-existing issues thoroughly and then fully dissolve the
god-module — no backward compatibility, no effort ceiling.

## Where we are today

**Two routes are dead, and the import graph explains why.** `POST /artifacts/restore` and
`PUT /artifact-annotations` reference their request models (`ArtifactRestoreRequest`,
`ArtifactAnnotationSaveRequest`) as *string* annotations that were never imported into the
`artifacts` route module. FastAPI resolves a body annotation via `get_type_hints` the first
time the route is exercised, so both endpoints raise `NameError` on every call — "restore a
file to a version" and "save image annotations" are broken in production. They were left that
way because the models live in `app.py`, and importing them into the route would have
deepened an already-massive back-import.

**The routes were split from `app.py` but never decoupled from it.** All eleven route modules
import *from* `app.py` — 316 names in total — while `app.py` imports the routes to mount them.
That is a genuine `app -> routes -> app` cycle that holds together only because `app.py` is
always the entry point and every name a route needs happens to be defined before the mount
loop runs. Worse, most of those 316 names are not even app-specific: routes re-import `re`
(91×), `asyncio` (10×), `httpx`, `Path`, `HTMLResponse`, and other stdlib/third-party symbols
*through* `app.py` rather than from their real source, plus a module-alias reach-through
(`from harness.server import app as _app`, then `_app._task_store`) at ~120 sites.

**`app.py` is a 4,162-line god-module.** It holds, in one file: 28 Pydantic request/response
DTOs; the SQLAlchemy `Base` and ten ORM records; 31 mutable runtime singletons (the task
store, registry, session factory, live executors, managers, the pub/sub broadcaster and
context event bus) rebound during a 174-line `lifespan`; ~90 helper functions spanning eight
unrelated concerns (artifacts, sessions, agents, projects, settings, mcp, filesystem,
terminals) plus a reverse-proxy rewriter; the agent-mounting hub; the config/agent/host
watchers; the auth middleware and CORS handler; and the `run_server` entry. There is no seam
a reader can hold; every concern reaches into every other through module globals.

**The lint contract has real breakage in it.** The project runs `ruff` with the default rule
set (there is no config, so that *is* the contract). Against it the tree carries 69 errors:
two `F821` undefined-names (the dead routes above — genuine bugs, not style), 31 `F401`
dead imports, and 36 `E402` import-ordering violations. A broader opt-in scan additionally
flags five `RUF006` fire-and-forget `asyncio.create_task` sites whose task references are
dropped — the event loop keeps only a weak reference, so those coroutines can be
garbage-collected mid-flight.

## The design

**Peel the data and state layers off first, into leaf modules.** `models.py` takes the 28
DTOs; `database.py` takes the `Base`, the ten ORM records, and the additive startup schema
reconciliation; `state.py` takes the 31 singletons plus the two small pub/sub primitives
(`Broadcaster`, the context event bus) they are built on. Each is a leaf that imports nothing
that imports it back (state's type-only imports sit under `TYPE_CHECKING`), so every other
module can depend on them freely. Extracting `models.py` is also what fixes the two dead
routes — the request models become importable from a module that is not behind the cycle.

**Break the cycle by inverting the dependency.** The route modules stop importing from the
entry module entirely: stdlib and third-party come from their real sources, re-exported
harness helpers come from *their* modules, DTOs from `models.py`, shared state from `state.py`
(read as `state.X`, an attribute access whose rebind is seen everywhere). The server body —
everything the routes call — moves into a module that imports **no** route. The old `app.py`
shrinks to a thin ASGI entry that imports the assembled `app`, mounts the route routers onto
it (the *one* place that imports routes), and exposes `run_server`. The dependency now flows
one way: `services -> state/database/models`, `boot -> services`, `asgi -> boot + routes`.

**Decompose the body by domain, mirroring the routes.** The ~90 helpers split into twelve
`services/` modules along the same concern lines as the route files — `artifacts`, `sessions`,
`agents`, `projects`, `settings`, `mcp`, `filesystem`, `terminals`, `remote_agents`, plus a
low-layer `broadcast` (the pub/sub helpers every domain uses), `locations` (the
location/project serialization primitives artifacts and projects share), and `proxy` (the
reverse-proxy rewriter cluster). The extraction order follows the call graph: leaf domains and
the shared low layers first, so that when a later domain calls an earlier one the dependency
is already a service import and never a back-reference into the body.

**Name the irreducible core for what it is.** What remains after every domain is a service is
the *composition root*: it builds the FastAPI `app`, wires the auth middleware and CORS
handler, mounts each agent's A2A sub-app, drives the startup/shutdown lifespan, and runs the
watchers. The `app` object, the `lifespan`, and the agent-mounting helpers are mutually
coupled through `app` and cannot be cleanly separated without threading it through parameters
for no benefit — so this is one cohesive module, renamed `boot.py` to name that role. The
entry file is renamed `asgi.py`: it no longer *defines* the app, it assembles the routers onto
it and exposes the ASGI callable (`harness.server.asgi:app`), mirroring the Django
`asgi.py`/`wsgi.py` convention. The `app` variable keeps its conventional name.

## Decided tradeoffs

**The boot smoke-test is what made the state move safe — so it gates every step.** The state
singletons are rebound during boot and read ~470 times (in the body and, as `_app.X`, in the
routes). Rewriting all of those to `state.X` is the kind of change where a single missed
rebinding breaks silently at runtime, invisible to a linter (a missed route-side `_app.X` is a
module-attribute access, not an undefined name). Two things de-risk it: removing the
singletons from the body entirely, so any missed *body* reference becomes an `F821` the linter
does catch; and a `TestClient` smoke-test that runs the **real** lifespan startup and shutdown
against a temp home and exercises live routes. That smoke-test — proven to work in this
environment before the state move began — turned "unverifiable without a running server" into
"verified every commit," and each of the twelve service extractions plus the state move was
landed only after it passed. **State stays a module of globals, not a holder object.** A
`state` module whose attributes the lifespan rebinds is the smallest change that preserves the
existing rebind-is-seen-everywhere semantics; a holder object would have been the same ~470
rewrites for no gain. **The composition root is not split further.** `boot.py` at ~690 lines
is not a god-module — it is a cohesive root — and fracturing app/lifespan/mounting apart would
trade real coupling for artificial indirection. **`database.py`, not `db.py`** — the module
and its references are spelled out, per direction. **No backward compatibility**: the module
boundaries, the import paths, and the entry name all change, because nothing outside the repo
depends on them.

**The broader lint scan is triaged, not blanket-applied.** The default `ruff` set is the
project's contract and is driven to zero. The opt-in scan's ~490 findings are mostly style
opinion the project never adopted and are deliberately *not* mass-applied; the one genuine
bug-risk class in it — the five `RUF006` dangling tasks — is fixed with a shared
`spawn_background_task` that retains a strong reference, and the two `RUF012`/one `B008`
findings are confirmed false positives (a Pydantic model's mutable default, a read-only class
constant, and the canonical FastAPI `File(...)` parameter).

## Testing

The bar is observed behaviour, driven by the boot smoke-test (`TestClient` over the real
lifespan against a temp home) plus `ruff` on every commit. For the dead-route fix: both
endpoints resolve their body model via `get_type_hints` instead of raising. For the cycle
break: no route imports `harness.server.asgi`; `boot.py` imports no route; no service imports
`boot`; and `boot`/`state`/`database` each import standalone with no cycle. For each service
extraction: `ruff` F401/F811/F821/E402 clean, and the smoke-test's lifespan boot + `/sessions`,
`/agents`, `/projects`, `/settings` all return non-5xx (exercising the sessions, agents,
projects, settings, locations, and remote-agents services through their real code paths). For
the dangling-task fix: the five sites schedule through the strong-reference helper and the
lifespan (which starts the remote-agent manager that way) still boots and shuts down cleanly.

## Implementation status (as built)

Landed and smoke-validated across the branch. The 4,162-line `app.py` is fully dissolved into
a layered architecture:

- **`asgi.py` (38 lines)** — the ASGI entry: import the assembled `app`, mount the route
  routers, expose `run_server`. The only module that imports routes.
- **`boot.py` (688 lines)** — the composition root: FastAPI app construction, auth middleware
  and CORS handler, agent A2A sub-app mounting, the lifespan, and the config/agent/host
  watchers. Imports no route.
- **`state.py` (172 lines)** — the 31 shared singletons (each carrying the rationale that used
  to sit orphaned in the body) plus `Broadcaster` and the context event bus.
- **`database.py` (252) / `models.py` (204)** — the SQLAlchemy layer and the HTTP DTOs.
- **`services/` (12 modules, ~3,187 lines)** — `artifacts` (637), `sessions` (476),
  `terminals` (445), `filesystem` (392), `projects` (288), `proxy` (259), `agents` (215),
  `locations` (186), `settings` (118), `mcp` (63), `remote_agents` (60), `broadcast` (48).

The two dead routes resolve their models and work. The default `ruff check` is clean across
the whole tree (the original 69 E402/F401/F821 all fixed), and the five `RUF006` dangling
tasks are closed via `harness.core.background_tasks.spawn_background_task`. The one deliberate
non-goal is the ~490 opt-in-rule findings, left unapplied as style the project never adopted.
