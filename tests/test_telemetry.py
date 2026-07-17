"""Telemetry facade: no-op when disabled, redaction, and span/traceparent behavior."""

import pytest

from harness.core import telemetry


@pytest.fixture
def in_memory_tracer():
    """Point the facade at an in-memory exporter so spans can be asserted without a
    network backend, then restore the disabled default."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    previous = telemetry._tracer
    telemetry._tracer = provider.get_tracer("test")
    try:
        yield exporter
    finally:
        telemetry._tracer = previous


def test_disabled_is_noop():
    telemetry._tracer = None
    assert telemetry.is_enabled() is False
    with telemetry.span("x", {"a": 1}) as active:
        assert active is None
    assert telemetry.current_traceparent() == ""
    assert telemetry.context_from_traceparent("00-abc-def-01") is None


def test_capture_posture():
    telemetry._capture = {"prompts": "redacted", "completions": "full", "tool_io": "off", "screenshots": "off"}
    assert telemetry.capture_text("here is api_key ABCDEFGHIJKLMNOP01", "prompts") == "here is api_key [redacted]"
    assert telemetry.capture_text("kept verbatim", "completions") == "kept verbatim"
    assert telemetry.capture_text("anything", "tool_io") is None


def test_spans_are_produced_and_nest(in_memory_tracer):
    assert telemetry.is_enabled() is True
    with telemetry.span("agent.turn", {"session.id": "ctx-1"}) as turn:
        telemetry.set_attributes(turn, {"gen_ai.usage.total_tokens": 42})
        with telemetry.span("tool.execute", {"tool.name": "bash"}):
            pass
    spans = in_memory_tracer.get_finished_spans()
    names = {span.name for span in spans}
    assert {"agent.turn", "tool.execute"} <= names
    turn_span = next(span for span in spans if span.name == "agent.turn")
    tool_span = next(span for span in spans if span.name == "tool.execute")
    assert turn_span.attributes["gen_ai.usage.total_tokens"] == 42
    assert turn_span.attributes["session.id"] == "ctx-1"
    # tool span nests under the turn span (same trace, turn is its parent)
    assert tool_span.parent is not None
    assert tool_span.parent.span_id == turn_span.context.span_id


def test_traceparent_round_trip(in_memory_tracer):
    with telemetry.span("agent.turn", {}):
        traceparent = telemetry.current_traceparent()
    assert traceparent.startswith("00-")
    assert telemetry.context_from_traceparent(traceparent) is not None
