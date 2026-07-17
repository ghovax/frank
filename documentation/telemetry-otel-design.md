# Design: User‑Capturable Telemetry via OpenTelemetry (OTLP)

**Status:** Design only — not yet implemented.
**Companion:** [`a2a-external-agents-design.md`](./a2a-external-agents-design.md) (remote‑agent calls become spans here).
**Framing (owner's words):** the goal is to *let users of Daisy capture Daisy's outgoing telemetry
into their own backend* — not to couple Daisy to any single vendor. Langfuse is one valid target,
not the design center.

---

## 1. Why this is a clean fit, not Frankenstein‑code

Daisy's model layer is **pure LangChain**: every provider flows through `build_chat_model`
(`agent.py:155`) into `ChatLiteLLMModel(BaseChatModel)` (`litellm_model.py:36`) or `ChatCodexModel`,
invoked via `ainvoke` (`agent.py:1329`, `:1380`, `:1578`) and `astream` (`agent.py:1948`). And the
A2A executor already gives us **natural trace boundaries for free**: one user turn = one A2A Task
(`a2a_executor.py:1101`), grouped by `context_id`, with delegated sub‑agents as related tasks.

So instrumentation is additive and idiomatic — no wrapping hacks, no shadow event system. Daisy also
already computes rich token/latency data (`token_usage` events, cumulative + per‑agent buckets,
`a2a_executor.py:1440`); telemetry **reuses** those numbers rather than recomputing them.

The one honest caveat: the agent loop is **hand‑rolled, not LangGraph**, so a bare LangChain callback
would capture individual generations but not the turn/tool/sub‑agent structure. We therefore own the
span tree explicitly (§4), which is exactly what makes the traces valuable.

## 2. Decisions locked (traceability)

| # | Decision | Choice |
|---|---|---|
| T1 | Emission mechanism | **OTLP / OpenTelemetry — any OTLP backend** (Langfuse, Phoenix, Grafana, Honeycomb, Datadog…) |
| T2 | Default posture | **Off until the user configures an endpoint** |
| T3 | Trace structure | **Turn = trace, `context_id` = session, delegated/remote agents = nested spans** |
| T4 | Redaction | **Redact sensitive by default; user can widen** |
| T5 | Trace context across the A2A wire | **Propagate W3C `traceparent` both ways** (emit outbound in message metadata, accept inbound) |

Rationale for **OTLP over the Langfuse SDK**: it satisfies the actual goal (users capture telemetry
in *their* backend) with **one** integration. Langfuse v3 is itself OTEL‑native, so nothing is lost
by going through OTLP; users who want Langfuse just point the OTLP exporter at Langfuse's endpoint.

---

## 3. What we emit

OpenTelemetry **traces** following the **GenAI semantic conventions** (`gen_ai.*` attributes), over
**OTLP** (HTTP/protobuf by default; gRPC optional). Signals:

- **Traces** (primary) — the turn/agent/tool/generation span tree (§4).
- **Metrics** (secondary, later phase) — token counters, model‑call latency histograms, per‑provider
  cost — derived from data already tracked.
- **Logs** — out of scope for v1 (Daisy already has server logging; not exported).

Deliberately **not** the Langfuse SDK, LangSmith, or a bespoke exporter — a single OTLP `TracerProvider`
with a user‑configured `OTLPSpanExporter`.

---

## 4. Span tree (T3)

```
trace  (root span)  = one A2A Task / user turn        [attrs: session.id = context_id, agent.name, task.id]
 └─ span  agent.turn
     ├─ span  gen_ai.generation   (each model call)   [gen_ai.request.model, usage tokens, latency, finish reason]
     ├─ span  tool.execute         (each tool call)   [tool.name, status; args/results redacted per T4]
     │    └─ span  tool.* details  (e.g. mcp.call, bash)
     └─ span  agent.delegate       (each sub‑agent)   [child.agent.name, local|remote]
          └─ (nested child turn spans — local in‑process OR remote via A2A client)
```

- **Trace boundary = the A2A executor turn** (`HarnessAgentExecutor.execute`, `a2a_executor.py:1101`).
  It opens the root span, stamps `session.id = context_id` (T3), `task.id`, agent name, and the turn
  kind (user / autonomous‑wake / compaction — the executor already distinguishes these).
- **Session = `context_id`** (T3), so a backend groups a whole conversation.
- **Generations** map to each `ChatLiteLLMModel`/`ChatCodexModel` call. Attach a Langfuse‑agnostic
  OTEL span around the call site; populate `gen_ai.usage.*` from the token numbers Daisy already
  emits (`a2a_executor.py:1440`).
- **Tools** wrap the runtime's tool dispatch (`agent.py:2885`, `:3300`, `:3732`).
- **Delegation** (`make_delegate`, `a2a_executor.py:1908`) opens a child span. With the external‑agent
  work, a **remote** delegation is the same span, tagged `local|remote` and carrying the remote
  agent id — so the companion A2A feature is observable end‑to‑end in one trace.

### Trace context across the A2A wire (T5)

When Daisy **calls a remote agent** (the external‑agent feature), it injects the current
W3C **`traceparent`** into the A2A message `metadata` so a shared OTLP backend can stitch Daisy's
`agent.delegate` span to the *remote* agent's own spans — one end‑to‑end trace across two systems.
Symmetrically, Daisy's **inbound** server reads a `traceparent` from an external caller and continues
that trace, so a third‑party client's trace links to the work Daisy does on its behalf. Propagation
is best‑effort: a peer that ignores or doesn't emit trace context simply yields two separate traces,
never an error. (This rides the same message `metadata` map used for the `urn:daisy:ext:turn:v1`
extension, kept under the standard `traceparent` key rather than the Daisy namespace.)

### Spans across a suspended / restarted turn

An `input-required` pause (A2A design D5/D17) can last minutes, hours, or span a restart. A span is
**not** held open across it: the turn span is **closed at the suspension** and a new span, **linked**
to it (OTEL span link), opens when the turn resumes. This keeps traces bounded and correct even when
a HITL answer arrives long later or after the server restarted.

### Async context propagation (the real implementation risk)

Sub‑agents and background wakes run as **separate asyncio tasks/coroutines**. OTEL context rides
`contextvars`, which do **not** auto‑propagate across `asyncio.create_task`. The design explicitly
carries the parent span context into delegated/background coroutines (attach on spawn) so the tree
nests correctly instead of producing orphaned root spans. This is the one part that needs care and a
test.

---

## 5. Where it plugs in

- **New module `harness/core/telemetry.py`** — owns the `TracerProvider`, reads config, builds the
  OTLP exporter, exposes thin helpers (`start_turn_span`, `record_generation`, `tool_span`,
  `delegate_span`) plus a **no‑op** implementation used whenever telemetry is disabled (T2), so call
  sites are unconditional and cost ~nothing when off.
- **Executor** (`a2a_executor.py`) opens/closes the turn span and threads context into sub‑turns.
- **Runtime** (`agent.py`) emits generation and tool spans at the existing call sites; feeds usage
  from the numbers already computed.
- **Config** — a `telemetry` block in `configuration.yaml` (+ `GlobalConfiguration`), and a
  Settings→(Observability) UI toggle, consistent with how providers/MCP are configured. Secrets
  (exporter headers/API keys for the user's backend) resolve from env like provider keys.

Config shape (illustrative):

```yaml
telemetry:
  enabled: false                       # T2: nothing emitted until set
  exporter:
    endpoint: "https://otlp.user-backend.example/v1/traces"
    protocol: "http/protobuf"          # or grpc
    headers: { Authorization: "${OTEL_EXPORTER_TOKEN}" }
  capture:
    prompts: redacted                  # T4: full | redacted | off
    completions: redacted
    tool_io: redacted
    screenshots: off                   # computer-use / browser DOM off by default
  sample_ratio: 1.0
```

---

## 6. Redaction (T4)

Redaction is applied **before export**, in `telemetry.py`, defaulting to *redact sensitive*:

- **Redacted by default:** file contents, computer‑use screenshots, browser DOM/text, and anything
  matching secret patterns (keys/tokens). These are the high‑risk payloads unique to Daisy's tool
  surface — the audit already showed the app treats similar fields as model‑only/heavy
  (`_MODEL_ONLY_RESULT_KEYS`, `_HEAVY_RESULT_KEYS`, `a2a_executor.py:340‑358`); the same instinct
  applies to what leaves the box as telemetry.
- **Captured by default:** prompts/completions text (redaction pass still strips detected secrets),
  token usage, latency, model id, tool names, finish reasons.
- **User can widen** to full fidelity or narrow to metadata‑only per the `capture` block.

Because the endpoint is user‑controlled, this is about *what Daisy puts in the payload*, not where it
goes — the user still owns egress.

---

## 7. Dependencies

- `opentelemetry-sdk`, `opentelemetry-exporter-otlp` (add to `pyproject.toml`).
- **No** Langfuse SDK dependency — deliberately. Langfuse is reached as an OTLP endpoint, keeping the
  integration vendor‑neutral.

## 8. Interaction with existing usage tracking

Daisy keeps its in‑app token/cost UI (`token_usage` events) — that's a *product* feature and stays
local. Telemetry is an *optional export* of the same underlying numbers plus prompt/latency/trace
structure for users who run an observability stack. No duplication of source‑of‑truth: both read the
same computed usage.

**Telemetry is a *harness*-level egress (note).** Because a harness can serve multiple clients and run
remotely, emission is configured on and performed by the harness, not per connected app. A shared
harness attributes all traces to that harness (grouped by `context_id`/session), not per end user —
worth stating plainly given the "users capture their telemetry" framing: it's *this harness's*
telemetry, wherever the harness runs.

## 9. Phasing

1. **Core traces:** `telemetry.py`, config + no‑op path, turn/generation/tool spans, session grouping.
2. **Async context propagation** for delegation/background wakes (+ its test).
3. **Redaction pass** (T4) and the `capture` config.
4. **Remote‑agent spans** — once external‑agent delegation exists, tag `local|remote` (§4).
5. **Metrics** (token counters, latency/cost histograms) — optional, later.
6. **UI toggle** in Settings→Observability.

## 10. Testing plan (deferred, specified)

- Export against a **local OTLP collector** in CI (deterministic, no SaaS) — assert the span tree
  shape, session grouping, and generation attributes.
- **Redaction unit tests:** screenshots/file‑contents/secrets never appear in exported spans at the
  default posture.
- **Context‑propagation test:** a delegated sub‑agent's spans nest under the parent turn, not as
  orphan roots.
- **Disabled‑path test:** with `enabled: false`, zero exporter calls and negligible overhead.
- Optional manual verification against a self‑hosted Langfuse and against Phoenix, to prove the
  "any OTLP backend" claim.

## 11. Recommendation

**Yes, do it — as vendor‑neutral OTLP, off by default, redacted by default.** It fits your LangChain
model layer and A2A turn structure cleanly (not Frankenstein), serves the stated goal (users capture
into *their* backend, Langfuse included) with a single integration, and preserves the local‑first
promise because nothing is emitted until the user opts in. Coupling directly to the Langfuse SDK
would be the *worse* choice here — more lock‑in, no benefit, since Langfuse ingests OTLP natively.

## 12. Open questions (for a later round)

- Traces‑only for v1, or ship metrics (token/cost/latency histograms) together?
- Sampling: always‑on (`sample_ratio: 1.0`) vs head sampling for heavy sessions?
- Do we also trace **harness server operations** (task store, artifact capture, MCP calls) or scope
  strictly to agent turns for v1? (Leaning: agent turns first; infra spans later.)
- Should the redaction default for prompt/completion **bodies** be `redacted` (strip detected secrets
  but keep text) or `off` (no bodies) for the most privacy‑conservative first release?
