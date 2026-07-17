"""User-capturable telemetry: OpenTelemetry traces exported over OTLP.

A process-wide facade. When disabled (the default) every helper is a cheap no-op; when the
user configures an OTLP endpoint, turns become traces (session grouped by the A2A
``context_id``), and model calls and tool calls become child spans following the GenAI
semantic conventions. Trace context rides the A2A message metadata as a W3C ``traceparent``
so a delegation nests under its parent turn and a shared backend can stitch Daisy's trace to
a remote agent's.

Nothing is emitted until an endpoint is set, so the local-first default stays quiet.
"""

import logging
import re
from contextlib import contextmanager
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

_tracer: Any = None
_capture: dict[str, str] = {"prompts": "redacted", "completions": "redacted", "tool_io": "redacted", "screenshots": "off"}

_REDACTED = "[redacted]"

# Coarse secret patterns stripped from captured text under the "redacted" posture: a
# secret-ish keyword followed by its value, provider key prefixes, and JWTs.
_SECRET_PATTERNS = [
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|bearer|access[_-]?token|client[_-]?secret)\b"
            r"(['\"]?\s*[:=]\s*['\"]?|\s+)([A-Za-z0-9_\-\.]{12,})"
        ),
        lambda match: f"{match.group(1)}{match.group(2)}{_REDACTED}",
    ),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9]{16,}\b"), lambda match: _REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), lambda match: _REDACTED),
]


def configure(
    *,
    enabled: bool,
    endpoint: str,
    headers: Optional[dict[str, str]] = None,
    capture: Optional[dict[str, str]] = None,
    sample_ratio: float = 1.0,
    service_name: str = "daisy",
) -> None:
    """Install (or tear down) the exporter. With no endpoint, telemetry stays disabled."""
    global _tracer, _capture
    if not enabled or not endpoint:
        _tracer = None
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name}),
        sampler=ParentBased(TraceIdRatioBased(max(0.0, min(1.0, sample_ratio)))),
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, headers=headers or None)))
    _tracer = provider.get_tracer("daisy")
    if capture:
        _capture = {**_capture, **capture}
    logger.info("Telemetry enabled; exporting traces to %s", endpoint)


def is_enabled() -> bool:
    return _tracer is not None


def _redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def capture_text(value: str, kind: str) -> Optional[str]:
    """Apply the configured capture posture for ``kind`` (prompts/completions/tool_io):
    ``off`` drops the body, ``redacted`` strips detected secrets, ``full`` keeps it."""
    mode = _capture.get(kind, "redacted")
    if mode == "off" or not value:
        return None
    return value if mode == "full" else _redact(value)


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
