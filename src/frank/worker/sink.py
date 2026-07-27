"""The consuming half of the turn-event catalog.

Every :class:`TurnEvent` the runtime yields is translated to its A2A wire part here — in a
single exhaustive, typed dispatch — and nowhere else. The sink owns the assistant-text
buffer, so a structured part forces an ordered flush, and it accumulates the turn's terminal
text and stop reason for the runner to read once the stream drains.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, assert_never


from frank.base import telemetry as _telemetry
from frank.protocol.events import (
    CompactionEvent,
    CumulativeUsage,
    ErrorEvent,
    McpEvent as McpWireEvent,
    PermissionRequestEvent,
    QuestionEvent,
    SteeringEvent,
    StatusEvent,
    ThinkingDoneEvent,
    ThinkingEvent,
    TokenUsageEvent,
    ToolCallEvent,
)
from frank.protocol.parts import _event_part, _text_part, _tool_result_part
from frank.runtime.turn_events import (
    Checkpoint,
    CompactionDone,
    CompactionStarted,
    DeniedInjection,
    Done,
    Error,
    Mcp,
    Status,
    Steering,
    Suspended,
    SuspensionGate,
    TextChunk,
    Thinking,
    ThinkingDone,
    ToolCall,
    ToolResult,
    TurnEventUnion,
    Usage,
)

class _TextPartBuffer:
    """Coalesce adjacent text chunks before publishing A2A task updates.

    The buffering is intentionally at the semantic event layer, not the SSE/ASGI
    layer: structured parts such as tool calls, status changes, and agent
    lifecycle events must force a flush so replay order remains exact.
    """

    def __init__(
        self,
        emit: Callable[[tuple[str, ...], str], Awaitable[None]],
        *,
        flush_interval: float = 0.041667,
        flush_size: int = 512,
    ):
        self._emit = emit
        self._flush_interval = flush_interval
        self._flush_size = flush_size
        self._key: tuple[str, ...] | None = None
        self._chunks: list[str] = []
        self._length = 0
        self._last_flush = 0.0

    async def push(self, text: str, key: tuple[str, ...] = ()) -> None:
        if not text:
            return
        if self._chunks and self._key != key:
            await self.flush(force=True)
        if not self._chunks:
            self._key = key
            self._last_flush = asyncio.get_running_loop().time()
        self._chunks.append(text)
        self._length += len(text)
        await self.flush()

    async def flush(self, force: bool = False) -> None:
        if not self._chunks or self._key is None:
            return
        now = asyncio.get_running_loop().time()
        if not force and self._length < self._flush_size and now - self._last_flush < self._flush_interval:
            return
        key = self._key
        text = "".join(self._chunks)
        self._key = None
        self._chunks = []
        self._length = 0
        self._last_flush = now
        await self._emit(key, text)


class _TurnEventSink:
    """The consuming half of the one turn-event catalog.

    Every :class:`TurnEvent` variant the runtime yields is translated to its A2A
    wire part here — in a single exhaustive, typed dispatch — and nowhere else. The
    sink owns the assistant-text buffer (so a structured part forces an ordered
    flush) and the turn's telemetry span, and it accumulates the turn's terminal
    text and stop reason for the orchestrator to read once the stream drains.

    A suspension goes to the injected ``suspend`` strategy, which returns whether the stream
    is finished: every pause is durable now — the segment closes and a later answer rebuilds
    the turn from its checkpoint — where a delegated turn used to park in place instead.
    """

    def __init__(
        self,
        *,
        emit: Callable[..., Awaitable[None]],
        save_conversation: Callable[[], Awaitable[None]],
        suspend: Callable[[list[SuspensionGate], dict], Awaitable[bool]],
        telemetry_span: Any,
        model_identifier: Callable[[], str],
    ) -> None:
        self._emit = emit
        self._save_conversation = save_conversation
        self._suspend = suspend
        self._span = telemetry_span
        self._model_identifier = model_identifier
        self._text = _TextPartBuffer(self._emit_text)
        self.final_text = ""
        self.stop_reason = ""

    async def _emit_text(self, key: tuple[str, ...], text: str) -> None:
        if not key:
            raise ValueError("Buffered assistant text is missing its content-block identity.")
        await self._emit(_text_part(text, key[0]))

    async def flush(self, force: bool = True) -> None:
        await self._text.flush(force=force)

    async def emit_compaction(self, event: CompactionStarted | CompactionDone) -> None:
        """Map a runtime compaction event to its ``compaction`` DataPart, so both the
        manual pass and mid-turn auto-compaction render identically (a live
        "compacting" indicator, then the separator)."""
        if isinstance(event, CompactionStarted):
            await self._emit(_event_part(CompactionEvent(
                status="started",
                reason=event.reason,
                messages_before=event.messages_before,
                tokens_before=event.tokens_before,
            )))
        elif isinstance(event, CompactionDone):
            await self._emit(_event_part(CompactionEvent(
                status="done",
                reason=event.reason,
                ok=event.ok,
                messages_before=event.messages_before,
                messages_after=event.messages_after,
                tokens_before=event.tokens_before,
            )))

    async def handle(self, event: TurnEventUnion) -> bool:
        """Consume one runtime event — emit its wire parts and advance turn state. Dispatch is
        a ``match`` over the closed :data:`TurnEventUnion`; the ``case _`` calls
        :func:`assert_never`, so a new variant a consumer forgets is a static exhaustiveness
        error, not a silently dropped branch (and a wiring bug at runtime if one slips through).
        Returns True when the turn should stop consuming and return from ``execute`` (a durable
        top-level suspension closed the segment), False to keep consuming."""
        match event:
            case TextChunk():
                content_block_identifier = str(event.block_id)
                if not content_block_identifier:
                    raise ValueError("Assistant text events require a content-block identity.")
                await self._text.push(event.text, (content_block_identifier,))
            case Thinking():
                await self.flush()
                await self._emit(_event_part(ThinkingEvent(text=event.text, block_id=event.block_id)))
            case ThinkingDone():
                await self.flush()
                await self._emit(_event_part(ThinkingDoneEvent(duration_ms=event.duration_ms)))
            case Status():
                await self.flush()
                await self._emit(_event_part(StatusEvent(code=event.code)))
            case ToolCall():
                await self.flush()
                await self._emit(_event_part(ToolCallEvent(
                    tool_name=event.name,
                    arguments=event.arguments if event.arguments is not None else {}, tool_call_id=event.id,
                )))
            case ToolResult():
                await self.flush()
                await self._emit(_tool_result_part(event.name, event.id, event.result, event.status))
            case Checkpoint():
                # A durable-safe point: snapshot the conversation so a mid-turn crash leaves
                # completed tools' results in the record (the next turn does not redo them).
                await self._save_conversation()
            case Mcp():
                await self.flush()
                await self._emit(_event_part(McpWireEvent(
                    server=event.server,
                    tool=event.tool,
                    event=event.event if event.event is not None else {},
                    tool_call_id=event.id,
                )))
            case Usage():
                await self.flush()
                cumulative = event.cumulative or {}
                model_identifier = self._model_identifier()
                _telemetry.set_attributes(self._span, {
                    "gen_ai.request.model": model_identifier or None,
                    "gen_ai.usage.input_tokens": cumulative.get("input_tokens", 0),
                    "gen_ai.usage.output_tokens": cumulative.get("output_tokens", 0),
                    "gen_ai.usage.total_tokens": cumulative.get("total_tokens", 0),
                    "gen_ai.model.calls": cumulative.get("model_calls", 0),
                })
                _telemetry.record_usage(model_identifier, event.input_tokens, event.output_tokens)
                await self._emit(_event_part(TokenUsageEvent(
                    input_tokens=event.input_tokens,
                    output_tokens=event.output_tokens,
                    context_window=event.context_window,
                    cumulative=CumulativeUsage(
                        input_tokens=cumulative.get("input_tokens", 0),
                        output_tokens=cumulative.get("output_tokens", 0),
                        total_tokens=cumulative.get("total_tokens", 0),
                        cache_read_tokens=cumulative.get("cache_read_tokens", 0),
                        reasoning_tokens=cumulative.get("reasoning_tokens", 0),
                        model_calls=cumulative.get("model_calls", 0),
                    ),
                )))
            case Suspended():
                # The turn needs one or more human decisions before it can run its tool batch.
                # Surface each gate as its native DataPart so a client renders the prompt, then
                # close the segment durably through the injected suspend strategy.
                await self.flush()
                interactions = event.interactions or []
                plans = event.plans or {}
                for gate in interactions:
                    if gate.kind == "question":
                        await self._emit(_event_part(QuestionEvent(
                            request_id=gate.request_id,
                            tool_call_id=gate.tool_call_id,
                            questions=gate.questions or [],
                        )))
                    else:
                        await self._emit(_event_part(PermissionRequestEvent(
                            request_id=gate.request_id,
                            tool_call_id=gate.tool_call_id,
                            command=gate.command, justification=gate.justification,
                            risk=gate.risk,
                        )))
                return await self._suspend(interactions, plans)
            case Error():
                await self.flush()
                await self._emit(_event_part(ErrorEvent(
                    message=event.message or "error", tool_call_id=event.id, tool_name=event.tool,
                )))
            case Steering():
                await self.flush()
                await self._emit(_event_part(SteeringEvent(text=event.text)))
            case CompactionStarted() | CompactionDone():
                await self.flush()
                await self.emit_compaction(event)
            case Done():
                await self.flush()
                self.final_text = event.text or self.final_text
                self.stop_reason = event.stop_reason or self.stop_reason
            case DeniedInjection():
                # A denied-command marker the runtime tracks for its own bookkeeping; the
                # executor does not surface it.
                pass
            case _:
                assert_never(event)
        return False
