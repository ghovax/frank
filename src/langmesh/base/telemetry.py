"""OpenTelemetry traces over OTLP: a cheap no-op until an endpoint is configured."""

from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

# The process-wide exporter, installed by `configure()` — what `langmeshd` sets up once at boot.
_tracer: Any = None
_token_counter: Any = None
_call_counter: Any = None

#: A tracer bound for one task, winning over the process-wide one: two sessions may report elsewhere.
_bound_tracer: contextvars.ContextVar[Any] = contextvars.ContextVar("langmesh_tracer", default=None)


def set_tracer(tracer: Any) -> contextvars.Token:
    """Make `tracer` the one this task's spans go to. Pair with :func:`reset_tracer`."""
    return _bound_tracer.set(tracer)


def reset_tracer(token: contextvars.Token) -> None:
    _bound_tracer.reset(token)


def _active_tracer() -> Any:
    """This task's tracer, else the process-wide one, else nothing."""
    return _bound_tracer.get() or _tracer


def _metrics_endpoint(traces_endpoint: str) -> str:
    """The metrics endpoint derived from the traces one, so a single configured URL covers both signals."""
    if traces_endpoint.endswith("/v1/traces"):
        return traces_endpoint[: -len("/v1/traces")] + "/v1/metrics"
    return traces_endpoint


def configure(
    *,
    enabled: bool,
    endpoint: str,
    headers: Optional[dict[str, str]] = None,
    sample_ratio: float = 1.0,
    service_name: str = "langmesh",
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
    provider = TracerProvider(
        resource=resource, sampler=ParentBased(TraceIdRatioBased(max(0.0, min(1.0, sample_ratio))))
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, headers=headers or None))
    )
    _tracer = provider.get_tracer("langmesh")

    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=_metrics_endpoint(endpoint), headers=headers or None)
    )
    meter = MeterProvider(resource=resource, metric_readers=[reader]).get_meter("langmesh")
    _token_counter = meter.create_counter(
        "gen_ai.client.token.usage", unit="{token}", description="LLM tokens used"
    )
    _call_counter = meter.create_counter(
        "gen_ai.client.operation.count", unit="{call}", description="LLM model calls"
    )
    logger.info("telemetry enabled, exporting traces and metrics to %s", endpoint)


def is_enabled() -> bool:
    return _active_tracer() is not None


def record_usage(model: str, input_tokens: int, output_tokens: int) -> None:
    """Record token counters and a model-call count for a completed model call."""
    if _token_counter is None:
        return
    _token_counter.add(
        max(0, input_tokens), {"gen_ai.request.model": model, "gen_ai.token.type": "input"}
    )
    _token_counter.add(
        max(0, output_tokens), {"gen_ai.request.model": model, "gen_ai.token.type": "output"}
    )
    _call_counter.add(1, {"gen_ai.request.model": model})


def start_span(name: str, attributes: Optional[dict[str, Any]] = None) -> Any:
    """Start a span without attaching it to the async context, so it is safe to hold across a ``yield``."""
    tracer = _active_tracer()
    if tracer is None:
        return None
    return tracer.start_span(name, attributes=attributes or {})


def end_span(active_span: Any, attributes: Optional[dict[str, Any]] = None) -> None:
    if active_span is None:
        return
    set_attributes(active_span, attributes or {})
    active_span.end()


@contextmanager
def span(
    name: str, attributes: Optional[dict[str, Any]] = None, parent_context: Any = None
) -> Iterator[Any]:
    """Open a span. Spans opened in the same task nest automatically."""
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(
        name, context=parent_context, attributes=attributes or {}
    ) as active_span:
        yield active_span


def set_attributes(active_span: Any, attributes: dict[str, Any]) -> None:
    if active_span is None:
        return
    for key, value in attributes.items():
        if value is not None:
            active_span.set_attribute(key, value)


def context_from_traceparent(traceparent: str) -> Any:
    """A parent context extracted from a ``traceparent`` header, or ``None``."""
    if _tracer is None or not traceparent:
        return None
    from opentelemetry.propagate import extract

    return extract({"traceparent": traceparent})


def record_client_fault(
    component: str, operation: str, attributes: Optional[dict[str, Any]] = None
) -> None:
    """Record a fault the interface handled, since a webview has no route to the collector of its own."""
    tracer = _active_tracer()
    if tracer is None:
        return
    active_span = tracer.start_span(
        "langmesh.client.fault",
        attributes={
            "langmesh.client.component": component,
            "langmesh.client.operation": operation,
            **(attributes or {}),
        },
    )
    active_span.end()
