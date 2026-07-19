---
created: 2026-07-18T08:48:57Z
updated: 2026-07-18T19:19:31Z
commit: 5859d9e
---

# A2A Specification Compliance Audit — Daisy Harness

**Scope:** how the Daisy harness uses the Agent2Agent (A2A) protocol, measured against the A2A specification **v0.3.0** as implemented by `a2a-sdk==0.3.7` (the exact version pinned in `pyproject.toml`). Every claim is cited to `file:line`.

**Bottom line up front.** Daisy is a *faithful, if minimal, A2A **server*** and — more unusually — it uses A2A's data types as the **internal vocabulary** for its own turn engine and agent delegation. It is **not** an A2A **client**. There is no code path anywhere in the repository that discovers, connects to, or delegates to an *external / third‑party* A2A agent. So the headline question — *"would connecting to an external agent be supported?"* — is **no, not today**, and closing that gap is the main piece of "plumbing architectural work" the codebase is missing. The good news: because delegation is already modeled in A2A types, the extension point is clean.

---

## 1. What the A2A spec (v0.3.0) actually requires

From the canonical types generated off the spec JSON schema (`a2a/types.py` in the SDK):

- **Three actors:** an *A2A Client*, an *A2A Server / Remote Agent*, and the *Task* as the stateful unit of work. Discovery is via an **AgentCard**.
- **AgentCard** (`AgentCard`, spec §5): `name`, `description`, `url`, `version`, `protocolVersion` (default `0.3.0`), `preferredTransport` (default `JSONRPC`), `additionalInterfaces[]`, `provider`, `iconUrl`, `documentationUrl`, `capabilities{streaming, pushNotifications, stateTransitionHistory, extensions[]}`, `securitySchemes{}`, `security[]`, `defaultInputModes[]`, `defaultOutputModes[]`, `skills[]`, `supportsAuthenticatedExtendedCard`, `signatures[]`.
- **Transports:** `JSONRPC`, `GRPC`, `HTTP+JSON` (`TransportProtocol` enum). A card MUST declare the transport at its main `url` via `preferredTransport`; other transports go in `additionalInterfaces`.
- **RPC methods** (`A2ARequest` union): `message/send`, `message/stream`, `tasks/get`, `tasks/cancel`, `tasks/resubscribe`, `tasks/pushNotificationConfig/{set,get,list,delete}`, `agent/getAuthenticatedExtendedCard`. (Note: there is **no** `tasks/list` method in v0.3.0.)
- **Task lifecycle** (`TaskState`): `submitted`, `working`, `input-required`, `completed`, `canceled`, `failed`, `rejected`, `auth-required`, `unknown`.
- **Streaming:** SSE stream of `Task` → `TaskStatusUpdateEvent` / `TaskArtifactUpdateEvent`, terminated by a `final: true` status event.
- **Content model:** `Message` → `Part` (`TextPart | FilePart | DataPart`); `Artifact` is the task's deliverable, also composed of `Part`s. Files travel as `FilePart` (`FileWithBytes` or `FileWithUri`).
- **Push notifications:** webhook config per task (`PushNotificationConfig`), gated behind `capabilities.pushNotifications`.
- **Security:** schemes declared in the card (`apiKey`, `http`, `oauth2`, `openIdConnect`, `mutualTLS`); `auth-required` task state supports in‑band credential requests.
- **Discovery:** the card is published at the well‑known URI `/.well-known/agent-card.json`.

---

## 2. How Daisy uses A2A today (the server side)

Daisy mounts **one A2A server per agent profile**. Each profile gets its own executor, its own `DefaultRequestHandler`, and its own AgentCard, mounted at a per‑agent path:

- `src/harness/server/app.py:1624` `_mount_agent()` — builds the card (`_card_for`, `app.py:1605`), constructs `HarnessAgentExecutor`, wraps it in the SDK's `DefaultRequestHandler(agent_executor=executor, task_store=_task_store)` (`app.py:1654`), and calls `A2AFastAPIApplication(...).add_routes_to_app(app, rpc_url=/a2a/agents/{name}, agent_card_url=/a2a/agents/{name}/.well-known/agent-card.json)` (`app.py:1658`).
- The **task store is custom**: `AppendOnlyTaskStore` (`src/harness/core/task_store.py:193`), a spec‑faithful drop‑in for the SDK's `DatabaseTaskStore` that fixes a real O(N²) write amplification problem (see the module docstring, `task_store.py:1`). It stores the exact A2A `Task`/`Message`/`Artifact` shapes, just normalized into append‑only rows. This is a genuinely good piece of engineering and stays fully within the A2A contract.
- A **turn = a Task.** The executor (`src/harness/core/a2a_executor.py:1101` `execute`) mints a task with `new_task(message)` and drives it with the SDK's `TaskUpdater`: `start_work()` → many `update_status(working, …)` → `add_artifact(name="result")` → `complete()` (`a2a_executor.py:1246`, `1141`, `1514`, `1529`). Stop → `cancel()`; error → `failed()`.
- The harness's own event vocabulary (streamed text, thinking, tool calls/results, agent activity, permission prompts, token usage) is carried **inside** A2A messages as typed `Part`s — `TextPart` for prose, `DataPart` for everything structured (`a2a_executor.py:302`, `309`; discriminator `data.kind`, `a2a_executor.py:120`). The live stream is therefore genuinely A2A‑shaped, not a bespoke side channel.
- **Extension metadata is done correctly.** Per‑turn harness metadata is namespaced under a single URI key `urn:daisy:ext:turn:v1` in `message.metadata`, exactly as the A2A extensions convention prescribes (`a2a_executor.py:76` and the citation in the comment above it).

**Discovery works for the default agent.** In addition to the per‑agent nested card path, there is a genuine **root** well‑known route: `routes/agents.py:162` serves the default agent's card at `/.well-known/agent-card.json` (the router is mounted with no prefix, `app.py:4045`), so an external client hitting the domain root gets a valid card. Non‑default agents are discoverable via the nested path or the non‑standard REST endpoint `GET /agents/cards` (`routes/agents.py:83`).

---

## 3. Compliance scorecard

| A2A feature | Status | Evidence |
|---|---|---|
| AgentCard served at well‑known URI | Yes (default agent at root; all agents at nested path) | `routes/agents.py:162`, `app.py:1661` |
| JSON‑RPC transport | Yes | `A2AFastAPIApplication`, `app.py:1658` |
| `message/send`, `message/stream` | Yes (via `DefaultRequestHandler`) | `app.py:1654` |
| `tasks/get`, `tasks/cancel`, `tasks/resubscribe` | Yes (via SDK handler + custom store) | `task_store.py:443`, `a2a_executor.py:1582` |
| SSE streaming of status/artifact events | Yes | `TaskUpdater` usage, `a2a_executor.py:1141`,`1514` |
| Artifact as deliverable | Yes (`name="result"`, `last_chunk=True`) | `a2a_executor.py:1514` |
| Task states: working / completed / canceled / failed | Yes | `a2a_executor.py:1246`,`1523`,`1527`,`1529` |
| `stateTransitionHistory` capability | Yes advertised & backed by append‑only history | `a2a_executor.py:297`, `task_store.py:216` |
| gRPC transport | No not implemented | only `A2AFastAPIApplication` mounted |
| HTTP+JSON / REST transport | No not implemented | ditto |
| `additionalInterfaces` in card | No omitted | `build_agent_card`, `a2a_executor.py:288` |
| `input-required` / `auth-required` task states | No never used | see §6 |
| Push notifications | No not wired (`push_config_store` is `None`) | `app.py:1654`; SDK default `None` |
| `pushNotifications` capability | Yes correctly advertised **false** (omitted) | `a2a_executor.py:297` |
| Security schemes / auth | No none declared, none enforced | `a2a_executor.py:288`; `README.md:104` |
| `FilePart` for files | No not used (images inlined as OpenAI blocks) | grep: no `FilePart` in `src/`; `a2a_executor.py:229` |
| `provider`, `iconUrl`, `documentationUrl` | No omitted | `a2a_executor.py:288` |
| Card `signatures` (JWS) | No omitted | `a2a_executor.py:288` |
| `agent/getAuthenticatedExtendedCard` | Partial supported by SDK but disabled (flag false) | card omits `supports_authenticated_extended_card` |
| **A2A client (outbound to external agents)** | No **absent entirely** | see §7 |

---

## 4. The AgentCard, field by field

`build_agent_card` (`src/harness/core/a2a_executor.py:254`) sets:

- `name` = agent identifier, `description`, `url` = `{base}/a2a/agents/{name}` (`a2a_executor.py:291`)
- `version = "1.0.0"`, `protocol_version = "0.3.0"`, `preferred_transport = "JSONRPC"` (`a2a_executor.py:292‑294`)
- `default_input_modes = ["text/plain"]`, `default_output_modes = ["text/plain"]` (`a2a_executor.py:295‑296`)
- `capabilities = AgentCapabilities(streaming=True, state_transition_history=True)` (`a2a_executor.py:297`)
- `skills` = discovered skills, or one synthesized default skill so the card always has ≥1 (`a2a_executor.py:269‑287`)

**Omitted** (all optional, but each is a real interoperability signal): `provider`, `icon_url`, `documentation_url`, `additional_interfaces`, `security_schemes`, `security`, `signatures`, `supports_authenticated_extended_card`, and `capabilities.push_notifications` (correctly left false) and `capabilities.extensions` (the `urn:daisy:ext:turn:v1` extension is **used** in message metadata but **not advertised** on the card — a minor honesty gap: a compliant client can't discover that the extension exists).

Two smaller notes:
- `default_output_modes` is `text/plain` only, yet the agent regularly produces rich artifacts (HTML, images, files). A stricter external client that negotiates on output modes would under‑estimate the agent.
- `version` is hard‑coded `"1.0.0"` for every agent; fine, but it's not tied to anything.

---

## 5. Transports

Only **JSON‑RPC 2.0** is mounted (`A2AFastAPIApplication`, `app.py:1658`). The SDK ships a REST app (`a2a/server/apps/rest/fastapi_app.py`) and a gRPC handler (`a2a/server/request_handlers/grpc_handler.py`), neither of which Daisy uses. Since only one transport exists and `preferredTransport=JSONRPC` matches the `url`, this is **compliant** (multi‑transport is optional). The only defect is that `additional_interfaces` is omitted rather than explicitly listing the single JSONRPC interface — a "SHOULD," not a "MUST."

---

## 6. Task lifecycle and the permission/question gap (the most substantive server‑side issue)

The executor only ever drives four states: `working`, `completed`, `canceled`, `failed` (`a2a_executor.py:1246`, `1529`, `1523`, `1527`/`1535`). It **never** uses `input-required` or `auth-required`.

This matters because Daisy *does* have human‑in‑the‑loop pauses — permission approvals and agent questions — and they are the textbook use case for `input-required`. Instead, Daisy models them as **custom `DataPart` events emitted while the task stays `working`**:

- `permission_request` DataPart (`a2a_executor.py:1462`) and `question` DataPart (`a2a_executor.py:1473`).
- Resolution happens **out of band**: the runtime blocks on an in‑memory `asyncio.Future` in `_pending_permissions` / `_pending_questions` (`app.py:377‑381`), which a **REST** endpoint resolves (the chat routes' permission/question handlers), not any A2A method.

Consequences:
- For Daisy's own client (the Tauri/Next.js app) this is fine and even ergonomic.
- For an **external** A2A client this is **not interoperable**: it would see a task sitting in `working` forever, carrying an opaque vendor `DataPart` it doesn't understand, with **no A2A‑native way to answer** (the answer channel is a private REST route). A spec‑literate peer expects `input-required` + a follow‑up `message/send` with the same `taskId`.

This is the one place where Daisy's "A2A as internal vocabulary" choice diverges from "A2A as an interoperable contract." It's a deliberate, reasonable trade‑off *today* (there are no external clients), but it's exactly the thing to revisit before opening the harness to third parties in either direction.

---

## 7. The core question — connecting to an *external* agent

**Not supported. There is no A2A client anywhere in the codebase.** Evidence: a repo‑wide search for every outbound A2A primitive — `A2AClient`, `ClientFactory`, `A2ACardResolver`, `from a2a.client`, remote `/.well-known/agent-card.json` fetches — returns **nothing** except one unused constant (`AGENT_CARD_PATH`, `app.py:108`). Daisy imports only A2A **server** and **types** modules.

What Daisy calls "delegation" / "spawn agent" is **in‑process**, not over the wire:

- `AgentRegistry` (`a2a_executor.py:1611`) holds a dict of local `RequestHandler` objects (`self._handlers`, `a2a_executor.py:1624`).
- `make_delegate()` (`a2a_executor.py:1908`) invokes another agent by calling `handler.on_message_send_stream(MessageSendParams(message=…))` **directly on the local Python object** (`a2a_executor.py:1948`) — same process, same event loop, no HTTP, no network.
- Inter‑agent messaging (`ask_agent` / `respond_agent`, `tools/tools.py:436`,`457`; `a2a_executor.py:1751`,`1810`) is an **in‑memory mailbox**. The code says so explicitly: *"A2A has no RPC for injecting a peer question into a running model call"* (`a2a_executor.py:1617‑1619`).

So the architecture is: **A2A types as the lingua franca for an in‑process multi‑agent runtime**, plus **an inbound A2A server surface**. The *outbound half of the protocol is simply not built.*

For completeness — **external connectivity today is done through MCP, not A2A.** `mcp_client.py` / `MCPClientManager` (`app.py:80`) is how Daisy reaches external tools/services, including hosted integrations like Composio (`composio_router.py`). MCP is "connect to external **tools**"; A2A would be "connect to external **agents**." Daisy has the former, not the latter.

### What "connect to an external agent" would require

The extension point is clean precisely because delegation already speaks A2A. Roughly:

1. **Add the A2A client dependency surface.** The pinned `a2a-sdk` already ships `a2a.client` (`ClientFactory`, `A2ACardResolver`, transports). Nothing new to install.
2. **A remote‑agent registry.** Let a user register an external agent by URL; fetch and cache its AgentCard via `A2ACardResolver`; store it alongside the local cards in `AgentRegistry`.
3. **Branch `make_delegate()` on locality.** If the target is a local handler → today's in‑process path; if it's a remote card → construct an `A2AClient` and stream `message/stream`, mapping the remote `TaskStatusUpdateEvent`/`TaskArtifactUpdateEvent`s back into the same relayed‑child events the parent already consumes (`a2a_executor.py:1952‑1996`). The parent's agents panel wouldn't know the difference.
4. **Expose it as a tool** (`connect_agent` / an external variant of `spawn_agent`) so the model can actually reach out.
5. **Handle the interop gaps that only matter across the wire:** authentication (send credentials per the remote card's `securitySchemes`), `input-required` round‑trips (a remote agent *will* use them), `auth-required`, content‑type negotiation, and `FilePart` (remote agents exchange files as `FilePart`, which Daisy neither emits nor parses today).
6. **Consider the inbound direction too:** if Daisy is to be a *good* external agent for *other* people's clients, fix §6 (surface permissions/questions as `input-required`) and optionally add auth on the JSON‑RPC endpoints (`README.md:104` confirms there is none today).

Items 1–4 are the "plumbing" you asked about and are modest — a few hundred lines — because the event‑relay and A2A‑typed delegation already exist. Items 5–6 are the harder, more valuable work and are where real spec‑compliance effort should go.

---

## 8. Parts, artifacts, files

- `TextPart` and `DataPart` are used pervasively and correctly (`a2a_executor.py:302`,`309`).
- The deliverable is a proper `Artifact` (`add_artifact`, `a2a_executor.py:1514`).
- **`FilePart` is never used** (no occurrences in `src/`). Image attachments are read and inlined as OpenAI‑shaped `image_url` data‑URI blocks for the model (`a2a_executor.py:229‑244`), and non‑image attachments travel as a custom `attachments` DataPart (`a2a_executor.py:192`). This is fine for Daisy's own client, but it means Daisy can neither **emit** nor **consume** files the A2A‑standard way — a real gap the moment an external agent is on either end of a file exchange.

---

## 9. Push notifications & security

- **Push:** `DefaultRequestHandler` is constructed with no `push_config_store` (`app.py:1654`; SDK default is `None`, confirmed in `a2a/server/request_handlers/default_request_handler.py:77`). The card correctly does **not** advertise `pushNotifications`, so this is internally consistent — the four `tasks/pushNotificationConfig/*` methods exist on the route but will reject. Push is the A2A‑native way to support long‑running/disconnected tasks; Daisy instead relies on its own SSE reconnection + resume‑pump machinery, which is fine for a co‑located client but is not the interoperable mechanism.
- **Security:** the card declares no `securitySchemes`/`security`, and there is **no auth** on the endpoints (`README.md:104` states this outright). Acceptable for a localhost‑default, single‑user tool; a blocker for any multi‑tenant or public exposure, and something an external peer's card‑driven auth would expect to negotiate.

---

## 10. Overall assessment — "is it using it properly?"

**Yes, for what it currently is; with a clear ceiling.** Daisy's use of A2A is coherent and, in places, tasteful:

- Using A2A `Task`/`Message`/`Part`/`Artifact` as the **internal** event model (rather than inventing a parallel one) is a strong choice — it keeps persistence, replay, and delegation all speaking one vocabulary, and the `AppendOnlyTaskStore` shows real care.
- The inbound server is spec‑valid JSON‑RPC with correct streaming, honest capability advertising, and correct extension‑metadata namespacing.

But it is currently a **one‑and‑a‑half‑legged** A2A implementation: a compliant **server**, an elegant **internal** type system — and **no client**, plus three interoperability gaps (`input-required`/`auth-required`, `FilePart`, security/auth) that don't bite in‑process but would immediately bite across the wire. The README bills A2A as a first‑class protocol Daisy "speaks"; strictly, Daisy speaks it *inward* and *to itself*, not *outward*.

### Recommended plumbing work, prioritized

1. **Decide the intent.** If "connect to external agents" is a product goal, the outbound client (§7 items 1–4) is the single highest‑leverage change and is architecturally cheap here.
2. **Model human‑in‑the‑loop as `input-required`** (§6). Even without external clients this makes the persisted task history more honest and is a prerequisite for interoperability.
3. **Adopt `FilePart`** for attachments/artifacts (§8) so files cross agent boundaries the standard way.
4. **Advertise the extension** (`capabilities.extensions = [urn:daisy:ext:turn:v1]`) and fill in `provider` / richer `default_output_modes` on the card (§4) — trivial, improves honesty.
5. **Add optional auth + declare `securitySchemes`** before any non‑localhost exposure (§9).
6. **(Optional) additional transports / push** only if a concrete consumer needs them — these are the lowest priority.

*No backward‑compatibility constraints apply (per the audit request), so `input-required` and `FilePart` adoption can be done as clean replacements rather than parallel paths.*
