# User-Capturable Telemetry via OpenTelemetry

This is the plan for letting people who run Daisy capture its telemetry in their own observability
backend. The goal is a generic export, not a coupling to any one vendor — Langfuse is a valid target,
not the design center. It shares a trace boundary with the external‑agent work in
[`a2a-external-agents-design.md`](./a2a-external-agents-design.md).

## Why this fits cleanly

Daisy's model layer is pure LangChain. Every provider flows through `build_chat_model`
(`agent.py:155`) into `ChatLiteLLMModel(BaseChatModel)` (`litellm_model.py:36`) or `ChatCodexModel`,
invoked via `ainvoke` (`agent.py:1329`, `agent.py:1380`, `agent.py:1578`) and `astream`
(`agent.py:1948`). The A2A executor already gives us natural trace boundaries: one user turn is one
A2A task (`a2a_executor.py:1101`), grouped by `context_id`, with delegated sub‑agents as related
tasks. And Daisy already computes rich token and latency data (`token_usage` events, cumulative and
per‑agent buckets, `a2a_executor.py:1440`), which telemetry reuses rather than recomputing.

So instrumentation is additive and idiomatic — no wrapping hacks, no shadow event system. The one
honest caveat is that the agent loop is hand‑rolled, not LangGraph, so a bare LangChain callback would
capture individual generations but not the turn/tool/sub‑agent structure. We therefore own the span
tree explicitly, which is exactly what makes the traces worth having.

## What we emit

OpenTelemetry traces following the GenAI semantic conventions (`gen_ai.*` attributes), over OTLP
(HTTP/protobuf by default, gRPC optional). The user points that OTLP endpoint at whatever they run —
Langfuse, Phoenix, Grafana, Honeycomb, Datadog, anything OTLP‑compatible. This is deliberately not the
Langfuse SDK: going through OTLP satisfies the actual goal with one integration, and Langfuse v3 is
itself OTEL‑native, so nothing is lost by pointing OTLP at it.

Traces are the primary signal. Metrics (token counters, latency and cost histograms, derived from data
we already track) come in a later phase. Logs stay out of scope; Daisy already has server logging and
we don't export it.

## The span tree

```
trace (root)  = one A2A task / user turn        session.id = context_id, agent.name, task.id
 └─ agent.turn
     ├─ gen_ai.generation   (each model call)    request model, usage tokens, latency, finish reason
     ├─ tool.execute        (each tool call)      tool name, status; args/results redacted
     │    └─ tool detail     (mcp.call, bash, …)
     └─ agent.delegate      (each sub-agent)      child agent name, local or remote
          └─ nested child turn spans (in-process locally, or over the A2A client remotely)
```

The trace boundary is the executor turn (`a2a_executor.py:1101`): it opens the root span and stamps the
session id from `context_id`, the task id, the agent name, and the turn kind (user, autonomous wake, or
compaction — the executor already distinguishes these). Generations wrap each model call and take their
usage numbers from what Daisy already emits (`a2a_executor.py:1440`). Tool spans wrap the runtime's
tool dispatch (`agent.py:2885`, `agent.py:3300`, `agent.py:3732`). Delegation opens a child span; with
the external‑agent work, a remote delegation is the same span tagged local or remote, so that feature
is observable end to end in one trace.

### Trace context across the A2A wire

When Daisy calls a remote agent, it injects the current W3C `traceparent` into the A2A message metadata
so a shared backend can stitch Daisy's `agent.delegate` span to the remote agent's own spans — one
end‑to‑end trace across two systems. Symmetrically, Daisy's inbound server reads a `traceparent` from an
external caller and continues that trace, so a third‑party client's trace links to the work Daisy does
for it. This is best effort: a peer that ignores or doesn't emit trace context just yields two separate
traces, never an error. It rides the same message metadata map used for the turn extension, under the
standard `traceparent` key rather than the Daisy namespace.

### Spans across a suspended or restarted turn

An `input-required` pause can last minutes, hours, or span a restart. We do not hold a span open across
it: the turn span closes at the suspension and a new span, linked to it, opens when the turn resumes.
That keeps traces bounded and correct even when a human answer arrives much later or after the server
restarted.

### Async context propagation (the real implementation risk)

Sub‑agents and background wakes run as separate asyncio tasks, and OTEL context rides `contextvars`,
which do not auto‑propagate across `asyncio.create_task`. We explicitly carry the parent span context
into delegated and background coroutines so the tree nests correctly instead of producing orphaned root
spans. This is the part that needs care and a dedicated test.

## Where it plugs in

A new `harness/core/telemetry.py` owns the tracer provider, reads config, builds the OTLP exporter, and
exposes thin helpers (start a turn span, record a generation, wrap a tool, wrap a delegation) plus a
no‑op implementation used whenever telemetry is disabled, so call sites are unconditional and cost
almost nothing when off. The executor opens and closes the turn span and threads context into
sub‑turns. The runtime emits generation and tool spans at the existing call sites and feeds usage from
the numbers already computed.

Configuration lives in a `telemetry` block in `configuration.yaml` (and `GlobalConfiguration`), plus a
Settings → Observability toggle, consistent with how providers and MCP are configured. Secrets — the
exporter headers or API key for the user's backend — resolve from the environment like provider keys.

```yaml
telemetry:
  enabled: false                       # nothing is emitted until this is set
  exporter:
    endpoint: "https://otlp.user-backend.example/v1/traces"
    protocol: "http/protobuf"          # or grpc
    headers: { Authorization: "${OTEL_EXPORTER_TOKEN}" }
  capture:
    prompts: redacted                  # full | redacted | off
    completions: redacted
    tool_io: redacted
    screenshots: off                   # computer-use / browser DOM off by default
  sample_ratio: 1.0
```

Nothing is emitted until the user configures an endpoint. This preserves the local‑first promise:
telemetry is opt‑in by configuration, and the machine stays quiet by default.

## Redaction

Redaction runs before export, in `telemetry.py`, defaulting to redact‑sensitive. By default we redact
file contents, computer‑use screenshots, browser DOM and text, and anything matching secret patterns —
the high‑risk payloads unique to Daisy's tool surface. The app already treats similar fields as
model‑only or heavy (`a2a_executor.py:340`), and the same instinct applies to what leaves the box as
telemetry. Captured by default are prompt and completion text (with a secret‑stripping pass), token
usage, latency, model id, tool names, and finish reasons. The user can widen to full fidelity or narrow
to metadata only through the capture block. Since the endpoint is user‑controlled, this governs what
Daisy puts in the payload, not where it goes — the user still owns egress.

## Dependencies

`opentelemetry-sdk` and `opentelemetry-exporter-otlp` are added to `pyproject.toml`. There is
deliberately no Langfuse SDK dependency: Langfuse is reached as an OTLP endpoint, keeping the
integration vendor‑neutral.

## Relationship to existing usage tracking

Daisy keeps its in‑app token and cost UI — that is a product feature and stays local. Telemetry is an
optional export of the same underlying numbers plus prompt, latency, and trace structure for users who
run an observability stack. Both read the same computed usage, so there is no second source of truth.

Telemetry is a harness‑level egress. Because a harness can serve multiple clients and run remotely,
emission is configured on and performed by the harness, not per connected app. A shared harness
attributes all traces to that harness (grouped by session), not per end user — worth stating plainly
given the framing: it is this harness's telemetry, wherever the harness runs.

## Build order

1. Core traces: `telemetry.py`, the config and no‑op path, turn/generation/tool spans, session
   grouping.
2. Async context propagation for delegation and background wakes, with its test.
3. The redaction pass and the capture config.
4. Remote‑agent spans and `traceparent` propagation, once external delegation exists.
5. Metrics (token counters, latency and cost histograms), optional.
6. The Settings → Observability toggle.

## Testing

Testing is deferred for now but planned. In CI we export against a local OTLP collector (deterministic,
no SaaS) and assert the span tree shape, session grouping, and generation attributes. Redaction unit
tests confirm that screenshots, file contents, and secrets never appear in exported spans at the
default posture. A context‑propagation test confirms a delegated sub‑agent's spans nest under the
parent turn rather than as orphan roots. A disabled‑path test confirms zero exporter calls and
negligible overhead when telemetry is off. Optionally we verify manually against a self‑hosted Langfuse
and against Phoenix, to prove the "any OTLP backend" claim.

## Recommendation

Do it, as vendor‑neutral OTLP, off by default, redacted by default. It fits the LangChain model layer
and the A2A turn structure cleanly, serves the goal of letting users capture into their own backend
with a single integration, and preserves the local‑first promise because nothing is emitted until the
user opts in. Coupling directly to the Langfuse SDK would be the worse choice — more lock‑in, no
benefit, since Langfuse ingests OTLP natively.

## Open questions

- Traces only for the first version, or ship metrics alongside.
- Sampling: always on, or head sampling for heavy sessions.
- Whether to also trace harness server operations (task store, artifact capture, MCP calls) or scope
  strictly to agent turns first (leaning: agent turns first).
- Whether prompt and completion bodies default to redacted (strip secrets, keep text) or off (no
  bodies) for the most conservative first release.
