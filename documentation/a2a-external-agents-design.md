# Connecting Daisy to External A2A Agents

This is the plan for making Daisy a full A2A participant in both directions: able to reach out to
third‑party A2A agents, and able to serve external A2A clients properly. It builds on the findings in
[`a2a-compliance-audit.md`](./a2a-compliance-audit.md) and shares a trace boundary with
[`telemetry-otel-design.md`](./telemetry-otel-design.md). There is no backward‑compatibility
constraint, so where a clean replacement beats a parallel path, we replace.

## Where we are today

Daisy is A2A server‑only. Each agent profile is mounted as its own JSON‑RPC endpoint with a served
AgentCard (`app.py:1624`, `app.py:1658`), backed by the SDK's `DefaultRequestHandler` and a custom
append‑only task store. What Daisy calls "delegation" is in‑process: `make_delegate()` invokes another
agent by calling `handler.on_message_send_stream(...)` directly on a local Python object
(`a2a_executor.py:1908`, `a2a_executor.py:1948`) — no network. There is no A2A client anywhere in the
codebase, so connecting to an external agent is simply not possible yet.

## The core idea

Everything upstream of `make_delegate()` — `spawn_agent`, the agents panel, task persistence, and the
relayed‑child event vocabulary (`a2a_executor.py:1952`) — stays exactly as it is. We make
`make_delegate()` **locality‑aware**: if the target is a local handler, take today's in‑process path;
if it's a registered remote agent, reach it over the wire with an A2A client. A remote agent becomes
"just another agent" to the model, and the whole panel/relay/persistence machinery is reused
unchanged.

```
delegate(agent_name, prompt, …)
   ├─ local  → handler.on_message_send_stream(…)          (in-process, unchanged)
   └─ remote → RemoteAgentClient.message_stream(…)        (a2a.client, over the wire)
```

## Outbound: the A2A client

### The remote-agent manager

A new `harness/core/remote_agents.py`, shaped like the existing `MCPClientManager` (`app.py:80`) — the
codebase's established pattern for managing external connections. It owns:

- **Card resolution and caching.** On registration it resolves the remote AgentCard with the SDK's
  `A2ACardResolver` (from `a2a.client`, already in the pinned `a2a-sdk`), validates it, and caches it
  with the fetch time. It re‑resolves on a configurable TTL or an explicit refresh. A failed
  resolution marks the agent unreachable (surfaced in the UI) but never crashes a turn.
- **Client construction.** It builds an `a2a.client` `Client` via `ClientFactory`, letting the client
  negotiate the transport from the card's `preferredTransport`/`additionalInterfaces`. So Daisy
  transparently speaks JSON‑RPC, gRPC, or HTTP+JSON to whatever the peer serves, without Daisy having
  to host gRPC or REST itself — we keep serving JSON‑RPC only.
- **Auth injection.** It attaches a client auth interceptor (`a2a.client.auth`) per the remote card's
  security schemes. We support a static API key / bearer token from config, and OAuth2
  client‑credentials (fetch, cache, and refresh a token against the scheme's token URL). mTLS is left
  for later.

### Locality-aware delegation

The `AgentRegistry` (`a2a_executor.py:1611`) gains a parallel registry of remote agents.
`make_delegate()` (`a2a_executor.py:1908`) branches on whether the name resolves to a local handler or
a remote client. For a remote agent it builds an A2A `Message` (role `user`, the delegated prompt, the
remote context mapped for this session) and streams `message/stream`, then maps the remote `Task` /
`TaskStatusUpdateEvent` / `TaskArtifactUpdateEvent` back into the same relayed event dicts the parent
already consumes (`a2a_executor.py:1952`). The agents panel cannot tell local from remote. Terminal
handling reuses the existing "hand back status + result artifact, drop history" contract
(`a2a_executor.py:1984`).

### Context boundary

We mint a new remote context id per (Daisy session, remote agent), kept in a mapping so multi‑turn
continuity works, and never leak Daisy's internal `context_id` to the peer. We send only the delegated
prompt plus any artifacts the caller explicitly references — resolved to file parts (see Files below).
No parent transcript leaves the machine.

### Egress consent

Contacting a remote agent sends data off the machine, so it is a permissioned action. Because the
human‑in‑the‑loop rework below removes the old REST permission channel, this consent is delivered as an
`input-required` pause — one unified mechanism, not a second one. First contact with a given remote
agent in a session prompts (allow / always‑allow / deny), with a risk level reflecting that data
leaves the machine; "always allow" is remembered per remote agent.

### Access control and the mailbox

Each agent profile declares an allow‑list of the remote agents it may call, alongside its existing
skills/tools scoping. The registry is global; use is scoped per profile, and a profile calling an
unlisted remote agent is refused before any network call.

Remote agents are delegation‑only. They can be spawned and delegated to, but they do not appear in
`active_agents` and cannot be targeted by `ask_agent`/`respond_agent`. The mailbox's contract is a
mid‑run injection into a live model call (`a2a_executor.py:1617`), for which A2A has no RPC; rather
than fake that over the wire, remote agents are simply absent from the mailbox. (Turning "ask" into a
fresh `message/send` on the shared remote context is possible later, but is out of scope for the first
version.)

One consequence to state plainly: a remote agent runs its own model on its own credentials, so its
token spend is not Daisy's and is not folded into the usage buckets. The UI shows a remote delegation
as an opaque sub‑task, not a metered one.

## Configuration and registration

The source of truth is a new `remote-agents.json`, a sibling to `mcp.json`, hot‑reloaded by the
existing file watcher (`app.py:1871`) exactly as MCP servers are:

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

Secrets resolve from the environment like existing keys, never inlined in tracked files. On top of the
file, we extend Settings → Connections (where remote harness and SSH connections already live) with a
"Remote agents" section: add by card URL, choose auth, test/refresh the card (showing the resolved
name, skills, transports, and health), and enable per profile. That UI is backed by new REST routes
and DB rows, consistent with how the file‑plus‑UI split already works for other connections.

## Inbound hardening

### Human-in-the-loop via input-required

A permission or question raises a request the runtime blocks on (a future the resolver completes), and
emits a vendor `DataPart`. The runtime blocking model keeps the connection open while it waits — the
paused turn holds the per‑context turn lock across the wait — so the answer is delivered without tearing
the turn down and rebuilding it.

On such a request the executor sets the task to `input-required`, carrying the request as its status
message, in addition to emitting the `DataPart`. An external A2A client sees the spec state and answers
with a `message/send` carrying an `input_response` part (the request id plus the decision or answers);
the executor routes that to the same pending future the resolver uses. This answer path runs before the
per‑context turn lock is taken — the paused turn holds that lock while awaiting the future, so taking it
there would deadlock against the very turn the answer unblocks. A naive peer that replies with plain
text simply doesn't resolve the request (the request stays open until answered, denied, or aborted).

The lifecycle a client observes is `working` → `input-required` (while paused) → `working` → terminal.
The native app's own resolution path is untouched, so its overlays keep working while external clients
gain the spec‑correct route.

### Server auth

We add an auth dependency in front of the mounted A2A routes (`app.py:1658`), configurable and off by
default for the localhost bundled case so zero‑config local use is untouched. Two schemes: a shared
API key / bearer secret validated per request, and OAuth2 / OIDC resource‑server validation against a
configured issuer or JWKS. When auth is enabled we advertise the matching security schemes on the
AgentCard (which currently declares none, `a2a_executor.py:254`) and tighten the permissive
`allow_origins=["*"]` CORS (`app.py:2204`).

### Files as FileWithUri

Files cross the wire as `FilePart` carrying a `FileWithUri`, the URI pointing at a Daisy HTTP endpoint
that streams bytes from the existing content‑addressed upload/artifact store (`routes/uploads.py:19`).
We use short‑lived signed URLs so a file link isn't an open door. Inbound, a `FilePart` (URI or bytes)
is fetched or decoded into the same upload store and handed to the model the way local attachments are
today (`a2a_executor.py:192`, `a2a_executor.py:229`). This finally gives Daisy real A2A file I/O, which
it lacks entirely right now.

### Push notifications

On the server side we construct `DefaultRequestHandler` with a real push‑config store and sender (both
are `None` today, `app.py:1654`), persist client webhook configs, POST task updates to them, and
advertise `pushNotifications` on the card. On the client side, when delegating to a remote agent we can
register a Daisy webhook so long‑running remote tasks notify us without a held stream, with an inbound
webhook receiver that resumes the parent turn on completion; we fall back to `tasks/resubscribe` when a
peer lacks push. Long‑running remote work integrates with the existing background/resume‑pump
machinery rather than inventing a parallel one.

### A more honest AgentCard

We fill the gaps the audit flagged in `build_agent_card` (`a2a_executor.py:254`): advertise the
`urn:daisy:ext:turn:v1` extension that we actually use, declare the JSON‑RPC interface in
`additionalInterfaces`, set `provider`, and broaden `defaultOutputModes` beyond `text/plain`.

## Data model

New tables alongside those in `app.py`:

- `remote_agents` — id, card URL, resolved name, auth kind and secret reference, cached card JSON, card
  fetch time, health, timestamps.
- `remote_agent_grants` — (agent profile, remote agent) allow‑list rows.
- `remote_contexts` — (session context id, remote agent) → remote context id mapping.
- `push_notification_configs` — inbound client webhook configs backing the SDK store.

The task store shape is unchanged: remote‑delegated children persist as related tasks exactly like
local ones (`referenceTaskIds`), so replay and history keep working.

## Security model

Outbound is defense in depth: a per‑profile allow‑list, then a per‑call permission prompt, then minimal
context egress, then short‑lived signed file URLs. Inbound is optional API‑key or OAuth2 auth,
advertised on the card, with CORS tightened when it's on. Secrets always resolve from the environment,
never tracked.

Trusting a remote card needs its own care. A card's `url` and `additionalInterfaces` can point
anywhere, so a malicious or compromised card could aim Daisy's requests at internal endpoints. We only
connect to hosts on the user's configured allow‑list, require the card's `url` host to match the
registration origin (so a card can't redirect Daisy elsewhere than the endpoint the user vetted), and
block private, loopback, and link‑local ranges unless the user explicitly opts a host in. This applies
to the initial card fetch, the RPC endpoint, and any push webhook target a peer supplies.

## Build order

1. **Spike.** Minimal outbound: resolve one card, build a client, stream `message/stream`, and map the
   events into the relay. Prove a remote reply lands in the agents panel.
2. **Outbound, productionized.** The `remote_agents` manager, card cache and TTL, auth (API key then
   OAuth2), locality‑aware `make_delegate`, per‑profile allow‑list, egress consent, remote‑context map.
3. **Configuration.** `remote-agents.json` plus the watcher, then the Settings → Connections UI with its
   routes and tables.
4. **Files.** Emit and accept `FileWithUri`, the signed serving endpoint, and inbound ingest.
5. **input-required.** The executor sets the `input-required` state and accepts `message/send` answers
   routed to the pending future, alongside the native resolution path.
6. **Server auth and card completeness.**
7. **Push, both directions.**
8. **Transports.** Confirm client negotiation across JSON‑RPC / gRPC / HTTP+JSON; keep the server on
   JSON‑RPC only.

Each phase is independently shippable and reviewable. The outbound client is the highest‑value,
lowest‑risk piece and directly answers the original question; the `input-required` rework is the most
invasive and deserves its own careful phase.

## Testing

Testing is deferred for now but planned. A tiny in‑repo echo agent (a second SDK server) gives
deterministic CI for the client, auth interceptors, event mapping, and file round‑trips without
external network. A Daisy‑to‑Daisy loopback exercises `input-required` across the wire and push
webhooks. Unit tests cover card resolution and TTL, allow‑list enforcement, the egress consent gate,
redacted context egress, OAuth token refresh, and signed‑URL expiry. A public reference agent is a
manual smoke test outside CI.

## Open questions

- The card TTL default, and whether health checks are polled or lazy (on use).
- Signed‑URL lifetime, and whether small files should fall back to inline bytes instead of a URL.
- Whether OAuth2 inbound implies any multi‑user notion or stays single‑principal (leaning
  single‑principal — auth as a gate, not identity).
