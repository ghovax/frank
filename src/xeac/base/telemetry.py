"""User-capturable telemetry: OpenTelemetry traces exported over OTLP.

A process-wide facade. When disabled (the default) every helper is a cheap no-op; when the
user configures an OTLP endpoint, a turn becomes a trace (session grouped by the A2A
``context_id``) carrying ``gen_ai.*`` usage attributes. Trace context rides the A2A message
metadata as a W3C ``traceparent`` so a delegation nests under its parent turn and a shared
backend can stitch Daisy's trace to a remote agent's.

Only span structure and usage/metadata are emitted — no prompt or completion bodies — so
there is nothing sensitive to redact. Nothing is emitted at all until an endpoint is set,
so the local-first default stays quiet.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

_tracer: Any = None
_token_counter: Any = None
_call_counter: Any = None


def _metrics_endpoint(traces_endpoint: str) -> str:
    """The OTLP metrics endpoint derived from the configured traces endpoint (the standard
    OTLP path pair), so a single configured URL covers both signals."""
    if traces_endpoint.endswith("/v1/traces"):
        return traces_endpoint[: -len("/v1/traces")] + "/v1/metrics"
    return traces_endpoint


def configure(
    *,
    enabled: bool,
    endpoint: str,
    headers: Optional[dict[str, str]] = None,
    sample_ratio: float = 1.0,
    service_name: str = "daisy",
) -> None:
    """Install (or tear down) the exporter. With no endpoint, telemetry stays disabled."""
    global _tracer, _token_counter, _call_counter
    if not enabled or not endpoint:
        _tracer = _token_counter = _call_counter = None
        return
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource, sampler=ParentBased(TraceIdRatioBased(max(0.0, min(1.0, sample_ratio)))))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, headers=headers or None)))
    _tracer = provider.get_tracer("daisy")

    reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=_metrics_endpoint(endpoint), headers=headers or None))
    meter = MeterProvider(resource=resource, metric_readers=[reader]).get_meter("daisy")
    _token_counter = meter.create_counter("gen_ai.client.token.usage", unit="{token}", description="LLM tokens used")
    _call_counter = meter.create_counter("gen_ai.client.operation.count", unit="{call}", description="LLM model calls")
    logger.info("Telemetry enabled; exporting traces and metrics to %s", endpoint)


def is_enabled() -> bool:
    return _tracer is not None


def record_usage(model: str, input_tokens: int, output_tokens: int) -> None:
    """Record token counters and a model-call count for a completed model call."""
    if _token_counter is None:
        return
    _token_counter.add(max(0, input_tokens), {"gen_ai.request.model": model, "gen_ai.token.type": "input"})
    _token_counter.add(max(0, output_tokens), {"gen_ai.request.model": model, "gen_ai.token.type": "output"})
    _call_counter.add(1, {"gen_ai.request.model": model})


def start_span(name: str, attributes: Optional[dict[str, Any]] = None) -> Any:
    """Start a span parented to the current context and return it (or ``None`` when
    disabled). Unlike :func:`span`, it does not attach the span to the async context, so it
    is safe to open across an async generator's ``yield``s; the caller ends it via
    :func:`end_span`."""
    if _tracer is None:
        return None
    return _tracer.start_span(name, attributes=attributes or {})


def end_span(active_span: Any, attributes: Optional[dict[str, Any]] = None) -> None:
    if active_span is None:
        return
    set_attributes(active_span, attributes or {})
    active_span.end()


@contextmanager
def span(name: str, attributes: Optional[dict[str, Any]] = None, parent_context: Any = None) -> Iterator[Any]:
    """Open a span (no-op when telemetry is disabled). Spans opened within the same async
    task nest automatically."""
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name, context=parent_context, attributes=attributes or {}) as active_span:
        yield active_span


def set_attributes(active_span: Any, attributes: dict[str, Any]) -> None:
    if active_span is None:
        return
    for key, value in attributes.items():
        if value is not None:
            active_span.set_attribute(key, value)


def current_traceparent() -> str:
    """The current span's W3C ``traceparent`` for propagation, or ``""`` when disabled."""
    if _tracer is None:
        return ""
    from opentelemetry.propagate import inject

    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier.get("traceparent", "")


def context_from_traceparent(traceparent: str) -> Any:
    """A parent context extracted from a ``traceparent`` header, or ``None``."""
    if _tracer is None or not traceparent:
        return None
    from opentelemetry.propagate import extract

    return extract({"traceparent": traceparent})
