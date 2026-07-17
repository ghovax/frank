# Design: Connecting Daisy to External A2A Agents (Full Bidirectional Overhaul)

**Status:** Design only — not yet implemented. Supersedes nothing; greenfield.
**Companion audit:** [`a2a-compliance-audit.md`](./a2a-compliance-audit.md).
**Companion design:** [`telemetry-otel-design.md`](./telemetry-otel-design.md) (traces remote‑agent calls as spans).
**No backward‑compatibility constraint** applies (owner's decision), so we replace rather than dual‑path where cleaner.

---

## 1. Goal

Make Daisy a *full* A2A participant in **both directions**:

- **Outbound (the headline gap):** Daisy can discover, connect to, and delegate to third‑party
  A2A agents over the wire, surfacing them to the model through the **existing delegation path**.
- **Inbound hardening:** Daisy becomes a first‑class A2A **server** for third‑party clients —
  spec‑correct human‑in‑the‑loop via `input-required`, real auth, `FilePart`, push notifications,
  and an honest AgentCard.

Today Daisy is server‑only and its "delegation" is in‑process (`a2a_executor.py:1908`,
`:1948`); there is **no** A2A client anywhere. See the audit for the full baseline.

## 2. Decisions locked (traceability)

| # | Decision | Choice |
|---|---|---|
| D1 | Scope | **Full bidirectional overhaul** |
| D2 | How remote agents surface to the model | **Extend the existing delegation path** (a remote agent is "just another agent") |
| D3 | Registration/config | **Both a config file and a Settings→Connections UI** |
| D4 | Outbound auth schemes | **API key/Bearer + OAuth2 (client credentials)** (mTLS deferred) |
| D5 | Inbound human‑in‑the‑loop | **Replace the REST side channel with A2A `input-required`** |
| D6 | Inbound server auth | **API key or OAuth2** (configurable; off for localhost) |
| D7 | Files on the wire | **`FileWithUri` served over Daisy's HTTP** (both emit & accept) |
| D8 | Push notifications | **Full push, both directions** |
| D9 | Server transports | **Serve JSONRPC only; client speaks all three** via `ClientFactory` |
| D10 | HITL answer encoding | **Namespaced `DataPart`** under `urn:daisy:ext:turn:v1` |
| D11 | Delivery | **Design doc first** (this); implementation later |
| D12 | Testing | **Deferred but planned** (see §11) |
| D13 | Egress consent | **Every outbound call gated by the permission system** (per‑agent always‑allow) |
| D14 | Access control | **Per‑profile allow‑list** of which remote agents a profile may call |
| D15 | Card discovery/refresh | **Fetch on register, cache with TTL + manual refresh** |
| D16 | Context sent to remote | **Delegated prompt + explicitly referenced artifacts only** |
| D17 | HITL pause across a server restart | **Exempt `input-required` from orphan-failing; persist the pending request and rehydrate on resume** |
| D18 | Remote agents & the mailbox | **Delegation‑only; remote agents are hidden from `active_agents` and cannot be `ask_agent`'d** (A2A has no mid‑run peer‑inject RPC) |
| D19 | Trust of a remote card's `url`/interfaces | **Host allow‑list + card‑origin check + block private/loopback ranges** (anti‑SSRF) |
| D20 | Egress prompt mechanism | **Egress consent (D13) is delivered as `input-required`, not the old REST channel** — one unified HITL path |

---

## 3. Architecture at a glance

```
                       ┌──────────────────────────────────────────────┐
                       │                Daisy harness                  │
  external A2A         │                                               │
  client  ───JSONRPC──▶│  A2AFastAPIApplication (per agent)            │
  (someone else)       │    DefaultRequestHandler ── AppendOnlyTaskStore│
                       │      HarnessAgentExecutor.execute()  (a task)  │
                       │        │                                       │
                       │        ├─ AgentRuntime (model + tools loop)    │
                       │        │                                       │
                       │        └─ delegate(agent_name, prompt, …)      │
                       │             │                                  │
                       │      ┌──────┴───────────────┐                  │
                       │  LOCAL: handler.on_message   REMOTE: RemoteAgentClient
                       │  _send_stream (in‑process)   (a2a.client, over wire)
                       └──────────────────────────────────┼─────────────┘
                                                          ▼
                                              external A2A agent (theirs)
```

The pivotal idea (D2): **`make_delegate()` becomes locality‑aware.** Everything upstream of it —
`spawn_agent`, `active_agents`, the agents panel, task persistence, the relayed‑child event
vocabulary (`a2a_executor.py:1952‑1996`) — is unchanged. A remote agent is reached by the same
`delegate(...)` call; only the transport under it differs.

---

## 4. Outbound: the A2A client

### 4.1 New module `harness/core/remote_agents.py`

Mirrors `mcp_client.py`'s `MCPClientManager` shape (the codebase's established "manager of external
connections" pattern, `app.py:80`). Responsibilities:

- **Card resolution & cache (D15).** On registration, resolve the remote AgentCard via the SDK's
  `A2ACardResolver` (from `a2a.client`, already in the pinned `a2a-sdk==0.3.7`). Validate it,
  store it, stamp a fetch time. Re‑resolve on a configurable TTL or an explicit "refresh" action.
  A resolution failure marks the agent `unreachable` (surfaced in UI, see §6) but never crashes a
  turn.
- **Client construction.** Build an `a2a.client` `Client` via `ClientFactory`, letting it negotiate
  the transport from the card's `preferredTransport`/`additionalInterfaces` — so Daisy transparently
  talks **JSONRPC, gRPC, or HTTP+JSON** to whatever the peer serves (D9), without Daisy hosting
  gRPC/REST itself.
- **Auth injection (D4).** Attach a client auth interceptor (`a2a.client.auth`) per the remote
  card's `securitySchemes`:
  - *API key / Bearer* — static header/token from config.
  - *OAuth2 client‑credentials* — fetch+refresh a token against the scheme's `tokenUrl`, cache it,
    refresh on expiry. (mTLS explicitly deferred.)
- **Handle map.** `remote_agent_id → (card, client, auth_state, health)`.

### 4.2 Locality‑aware delegation

`AgentRegistry` (`a2a_executor.py:1611`) gains a parallel registry of remote agents. `make_delegate`
(`a2a_executor.py:1908`) branches:

- **Local** (name resolves to `self._handlers`): today's in‑process
  `handler.on_message_send_stream(...)` path — unchanged.
- **Remote** (name resolves to a `RemoteAgentClient`): construct an A2A `Message`
  (role `user`, the delegated prompt, `contextId` = the remote context mapped for this session,
  D16) and call the remote client's streaming `message/stream`. Map the remote
  `Task` / `TaskStatusUpdateEvent` / `TaskArtifactUpdateEvent` back into the **exact same** relayed
  event dicts the parent already consumes (`a2a_executor.py:1952‑1996`). The agents panel cannot
  tell local from remote.

Remote task terminal handling reuses the existing "hand back status + result artifact, drop
history" contract (`a2a_executor.py:1984‑1995`).

### 4.3 Context boundary (D16)

- Mint a **new remote `contextId` per (Daisy session, remote agent)**, kept in a mapping table so
  multi‑turn continuity works. Never leak Daisy's internal `context_id` to the peer.
- Send **only** the delegated prompt plus artifacts the caller explicitly references; resolve those
  to `FilePart`s (§7). No parent transcript egress.

### 4.4 Egress permissioning (D13, D20)

Contacting a remote agent is a **permissioned action**. Because the HITL migration (D5) removes the
old REST channel, egress consent is delivered as an **`input-required`** pause (D20), not the legacy
`_pending_permissions` route — one unified human‑in‑the‑loop mechanism (see §6.1). First contact with
a given remote agent in a session prompts (allow / always‑allow / deny), with a risk level reflecting
that data leaves the machine. "Always allow" is remembered per remote agent.

### 4.5 Access control (D14) and mailbox exclusion (D18)

Each agent **profile** declares an allow‑list of remote agents it may call (alongside its existing
skills/tools scoping). The remote registry is global (server‑wide); *use* is scoped per profile.
A profile calling an unlisted remote agent is refused before any network call.

**Remote agents are delegation‑only (D18).** They can be spawned/delegated, but they do **not** appear
in `active_agents` and cannot be targeted by `ask_agent`/`respond_agent`. The mailbox's contract is a
*mid‑run injection into a live model call* (`a2a_executor.py:1617`), for which A2A has no RPC; rather
than fake it over the wire, remote agents are simply absent from the mailbox. (A future "ask = new
`message/send` on the shared remote context" is possible but explicitly out of scope for v1.)

**Cost is opaque (note):** a remote agent runs its own model on its own credentials, so its token
spend is not Daisy's and is not folded into the `token_usage`/agents buckets — the UI shows a remote
delegation as an opaque sub‑task, not a metered one.

---

## 5. Configuration & registration (D3 — both file and UI)

### 5.1 File — source of truth

A new `remote-agents.json` (sibling to `mcp.json`), hot‑reloaded by the **existing file watcher**
(`app.py:1871`) exactly as MCP servers are. Shape (illustrative):

```jsonc
{
  "agents": {
    "acme-researcher": {
      "cardUrl": "https://agents.acme.com/.well-known/agent-card.json",
      "auth": { "type": "bearer", "token": "${ACME_TOKEN}" },   // or oauth2 { tokenUrl, clientId, clientSecret, scopes }
      "cardTtlSeconds": 3600
    }
  }
}
```

Secrets resolve from env like existing keys (`.env` / `configuration.yaml`), never inlined in tracked files.

### 5.2 UI — editor on top

Extend **Settings → Connections** (where remote harness/SSH connections already live) with a
"Remote agents" section: add by card URL, choose auth, test/refresh the card (shows resolved name,
skills, transports, health), enable per profile. Backed by new REST routes and DB rows (§8) —
consistent with the "both file and UI" pattern already chosen for MCP.

---

## 6. Inbound hardening

### 6.1 Human‑in‑the‑loop via `input-required` (D5, D10) — the biggest change

Today a permission/question pauses the task in `working` and emits a vendor `DataPart`, resolved by
a private REST call (`a2a_executor.py:1460‑1481`; `routes/chat.py`; futures in `app.py:377‑381`).
**Replace this** with the spec state machine:

1. When the runtime needs approval/answer, the executor transitions the task to
   **`TaskState.input_required`** and emits the request as a namespaced `DataPart` in the
   `input-required` status message (D10: `urn:daisy:ext:turn:v1` payload carrying the permission
   command/risk or the question schema).
2. The turn **suspends** (the task is not terminal; the SSE stream ends with a non‑final
   `input-required` status per spec).
3. The client — **including Daisy's own native app** — answers with a fresh **`message/send` /
   `message/stream`** carrying the same `taskId` and a namespaced `DataPart` encoding the decision
   (`allow`/`deny`/`always`) or the structured answers. A naive external peer may reply with plain
   text; the runtime tolerates that as a fallback.
4. The executor resumes the suspended runtime with the answer and drives the task forward.

Consequences (accepted, no backward‑compat constraint):
- The REST permission/question endpoints and the `_pending_permissions`/`_pending_questions` future
  maps are **removed**; resolution flows through the normal message path.
- **Frontend rework:** the permission/question overlays (`permission-overlay.tsx`,
  `question-overlay.tsx`) now submit a `message/send` resuming the task rather than POSTing to the
  side‑channel route. The `streamA2A` client (`web/src/lib/api.ts`) grows an "answer input‑required"
  call. This is the main UI cost and is called out as its own phase (§10).
- Runtime suspension/resume must survive the SSE stream closing (the client may answer minutes
  later or after reconnect) — the suspended state is keyed on the task and rehydratable, which the
  append‑only task store already supports for replay.

**Survives a server restart (D17).** Today `fail_orphaned_tasks` (`task_store.py:282`) fails every
non‑terminal task on startup — which would silently kill a legitimately long `input-required` pause.
`input-required` is therefore **exempted**: the pending request (permission command/risk or question
schema) is persisted, and on restart the task is rehydrated as still‑suspended rather than failed;
answering it resumes the runtime as normal. This is the one place the orphan‑failing rule gets a
carve‑out, and it needs a test.

**Telemetry interaction:** a suspension can outlast a turn (or a restart), so the OTEL turn span is
**closed at the pause** and a **linked** span opens on resume — no span is held open across an
indefinite wait (see [`telemetry-otel-design.md`](./telemetry-otel-design.md) §4).

### 6.2 Server auth (D6)

Add an auth dependency in front of the mounted A2A routes (`app.py:1658` mount site). Two schemes,
configurable, **off by default for the localhost bundled case** (preserving zero‑config local use):
- **API key / Bearer** — shared secret in config; validated per request.
- **OAuth2 / OIDC resource server** — validate bearer tokens against a configured issuer/JWKS.

When enabled, advertise the matching `securitySchemes` + `security` on the AgentCard
(`build_agent_card`, `a2a_executor.py:254`), which currently declares none. Tighten the permissive
`allow_origins=["*"]` CORS (`app.py:2204`) when auth is on.

### 6.3 Files as `FileWithUri` (D7)

- **Emit:** artifacts/attachments cross the wire as `FilePart{ FileWithUri }`, the URI pointing at
  a Daisy HTTP endpoint that streams bytes from the existing content‑addressed upload/artifact store
  (`routes/uploads.py:19‑85`; artifact serving already exists). Add short‑lived signed URLs so a
  file link isn't an open door.
- **Accept:** an inbound `FilePart` (URI or bytes) is fetched/decoded into the same upload store and
  handed to the model the way local attachments are today (`a2a_executor.py:192`, `:229`). This
  finally gives Daisy real A2A file I/O (currently `FilePart` is unused entirely).

### 6.4 Push notifications, both directions (D8)

- **Server:** construct `DefaultRequestHandler` with a real `PushNotificationConfigStore` +
  `BasePushNotificationSender` (today both are `None`, `app.py:1654`), persist configs, and POST
  task updates to client‑registered webhooks. Advertise `capabilities.pushNotifications = true`.
- **Client:** when delegating to a remote agent, optionally register a Daisy webhook so long‑running
  remote tasks notify us without a held stream; add an inbound webhook receiver route that resumes
  the parent turn on remote completion. Falls back to `tasks/resubscribe` when a peer lacks push.

### 6.5 AgentCard completeness

Fill the honest‑signal gaps the audit flagged (`build_agent_card`, `a2a_executor.py:288`): advertise
the `urn:daisy:ext:turn:v1` **extension**, declare the single JSONRPC interface in
`additional_interfaces`, set `provider`, and broaden `default_output_modes` beyond `text/plain`.

---

## 7. Data model changes

New tables (SQLAlchemy, alongside those in `app.py`):

- `remote_agents` — id, card_url, resolved name, auth kind + secret ref, card JSON cache, card
  fetched_at, health, created/updated.
- `remote_agent_grants` — (agent_profile, remote_agent_id) allow‑list rows (D14).
- `remote_contexts` — (session context_id, remote_agent_id) → remote contextId mapping (D16).
- `push_notification_configs` — inbound client webhook configs (D8), backing the SDK store.

The A2A task store (`task_store.py`) is unchanged in shape: remote‑delegated children persist as
related tasks exactly like local ones (`referenceTaskIds`), so replay/history "just works."

---

## 8. Security model (summary)

- **Outbound:** per‑profile allow‑list (D14) → per‑call permission prompt (D13) → minimal context
  egress (D16) → signed short‑lived file URLs (§6.3). Defense in depth against accidental data
  exfiltration to a remote agent.
- **Card / URL trust (D19, anti‑SSRF):** a remote AgentCard's `url` and `additionalInterfaces` can
  point anywhere, so they are not trusted blindly. Daisy only connects to hosts on the user's
  configured allow‑list, requires the card's `url` host to match the registration origin (a card
  can't redirect Daisy to a different endpoint than the one the user vetted), and blocks
  private/loopback/link‑local IP ranges unless the user explicitly opts a host in. This applies to
  the initial card resolution, the RPC endpoint, and any push webhook target the peer supplies.
- **Inbound:** optional API‑key/OAuth2 auth (D6), advertised in the card, CORS tightened when on.
- **Secrets** resolve from env, never tracked.

---

## 9. Phasing (implementation order, when we build it)

1. **Spike (de‑risk):** minimal outbound — resolve one card, `ClientFactory` client, stream
   `message/stream`, map events into the relay. Prove a remote reply lands in the agents panel.
   *(This was the recommended delivery mode; we're doing design‑first, but this is still the right
   first build step.)*
2. **Outbound productionized:** `remote_agents.py` manager, card cache/TTL, auth (API key → OAuth2),
   locality‑aware `make_delegate`, per‑profile allow‑list, egress permissioning, remote‑context map.
3. **Config surface:** `remote-agents.json` + watcher; then Settings→Connections UI + REST + DB.
4. **FilePart I/O:** emit/accept `FileWithUri`, signed serving endpoint, inbound ingest.
5. **Inbound `input-required`:** runtime suspend/resume + executor state machine + **frontend
   overlay rework** + remove REST side channel.
6. **Server auth + card completeness.**
7. **Push, both directions.**
8. **Transports:** confirm client negotiation across JSONRPC/gRPC/HTTP+JSON; keep server JSONRPC‑only.

Each phase is independently shippable and reviewable.

## 10. Testing plan (deferred per D12, specified now)

- **In‑repo echo/test A2A agent** (a tiny second server using the SDK) for deterministic CI of the
  client, auth interceptors, event mapping, and FilePart round‑trips — no external network.
- **Daisy‑to‑Daisy loopback:** one Daisy instance delegates to another as a realistic peer; exercises
  `input-required` across the wire and push webhooks.
- **Unit:** card resolution/cache/TTL, allow‑list enforcement, egress permission gate, redacted
  context egress, OAuth token refresh, signed‑URL expiry.
- **Manual smoke** against a public reference A2A agent, out of CI.
- Correctness first; wire these once the modules exist.

## 11. Open questions (for a later round)

- OAuth2 inbound: does accepting external‑identity tokens imply any **multi‑user** notion, or stays
  single‑principal? (Leaning single‑principal; auth is a gate, not identity.)
- Card TTL default value and whether health‑checks are active (polled) or lazy (on‑use).
- Signed‑URL lifetime and whether remote peers can be trusted to fetch within it, vs falling back to
  `FileWithBytes` for small files.
- *(Resolved D18 — remote agents are delegation‑only, hidden from the mailbox in v1.)*
