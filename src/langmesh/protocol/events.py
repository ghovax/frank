"""The single source of truth for streamed events and model-facing envelopes, from which the TypeScript is generated."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field


# Shared building blocks


class ToolStatus(str, Enum):
    RUNNING = "running"  # accepted / in flight (a backgrounded command, a live search)
    OK = "ok"            # finished successfully
    ERROR = "error"      # failed, denied, or cancelled (see `code` for which)


def tool_status_from_result(result: Any) -> ToolStatus:
    """Read a result's explicit lifecycle status, defaulting synchronous results to OK."""
    record = result if isinstance(result, dict) else {}
    explicit = record.get("status")
    if explicit in (ToolStatus.RUNNING.value, ToolStatus.OK.value, ToolStatus.ERROR.value):
        return ToolStatus(explicit)
    return ToolStatus.OK


class ToolMetadata(BaseModel):
    """Correlational and timing facts about a tool call, kept visible to the model as well as the interface."""

    tool_name: str
    tool_call_id: str
    started_at: str | None = None
    completed_at: str | None = None
    duration_milliseconds: int | None = None
    # Present only for work that was handed to the background runner (bash/search).
    background_job_id: str | None = None


# Every streamed event is a payload of the shape `{kind, ...}`, carrying no tree position of its own.

class _EventBase(BaseModel):
    """Base of the wire-event union. Every event contributes its own `kind` literal."""

    #: When this event was made, on every event because which ones want a time is not knowable in advance.
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class TextEvent(_EventBase):
    kind: Literal["text"] = "text"
    text: str


class ThinkingEvent(_EventBase):
    kind: Literal["thinking"] = "thinking"
    text: str = ""
    # The reasoning block this chunk belongs to, so a streamed thinking block coalesces rather than appending lines.
    block_id: str = ""


class ThinkingDoneEvent(_EventBase):
    kind: Literal["thinking_done"] = "thinking_done"
    duration_milliseconds: int = 0


class ToolCallEvent(_EventBase):
    kind: Literal["tool_call"] = "tool_call"
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResultEvent(_EventBase):
    kind: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    tool_name: str
    status: ToolStatus
    # A finer outcome than `status` where one is needed, such as cancelled rather than a generic error.
    code: str | None = None
    # Whatever the interface should render, shaped by the tool; the model reads its result from the conversation.
    display: Any = None
    metadata: ToolMetadata


class McpEvent(_EventBase):
    kind: Literal["mcp_event"] = "mcp_event"
    tool_call_id: str
    server: str = ""
    tool: str = ""
    event: dict[str, Any] = Field(default_factory=dict)


class StatusEvent(_EventBase):
    kind: Literal["status"] = "status"
    code: str = ""


class TracedSegment(BaseModel):
    """Which piece of a request a cache measurement is about, as fields rather than a formatted label."""

    kind: str = ""
    position: int = -1
    role: str = ""


class PrefixDivergence(BaseModel):
    """Where a request stopped matching the one before it."""

    index: int = 0
    #: What occupies that position now. Absent when the request simply got shorter there.
    current: Optional[TracedSegment] = None
    #: What occupied it on the previous call.
    previous: TracedSegment = Field(default_factory=TracedSegment)
    #: The piece did not move; its contents changed. The only kind usually worth chasing.
    rewritten: bool = False


class CumulativeUsage(BaseModel):
    """Session-lifetime running totals, distinct from the per-call figures that describe only the latest call."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    #: What a cache could have returned across the session, so the read figure has a denominator.
    reachable_tokens: int = 0
    reasoning_tokens: int = 0
    model_calls: int = 0


class DoneEvent(_EventBase):
    kind: Literal["done"] = "done"
    # The terminal task state for the agent at `path` ("completed"/"failed"/...).
    state: str = "completed"


class CompactionEvent(_EventBase):
    kind: Literal["compaction"] = "compaction"
    status: Literal["started", "done"]
    reason: str = ""
    ok: bool = True
    messages_before: int = 0
    messages_after: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    log_tokens: int = 0


class SteeringEvent(_EventBase):
    kind: Literal["steering"] = "steering"
    text: str = ""
    #: The session that sent this, when it was not the person.
    peer_sender: str = ""
    #: The id the sender gave this message, so a client can recognise the message it already showed.
    message_id: str = ""


class TokenUsageEvent(_EventBase):
    kind: Literal["token_usage"] = "token_usage"
    # Per-call (latest model call) figures — the current context, not a sum.
    input_tokens: int = 0
    output_tokens: int = 0
    context_window: int = 0
    # Per-call cache and reasoning, because a running total cannot say which call missed.
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    # Session-lifetime running totals for this agent's own calls.
    cumulative: CumulativeUsage = Field(default_factory=CumulativeUsage)
    # What the cache figure means, which the figure alone cannot say: a moved prefix, or one the provider dropped.
    prefix_intact: bool = False
    reachable_tokens: int = 0
    segments: int = 0
    shared_segments: int = 0
    divergence: Optional[PrefixDivergence] = None


class PermissionReason(BaseModel):
    """Why approval is needed, as data rather than a sentence, so a client can say it in its own language."""

    kind: str
    paths: list[str] = Field(default_factory=list)


class PermissionRequestEvent(_EventBase):
    kind: Literal["permission_request"] = "permission_request"
    request_id: str
    tool_call_id: str = ""
    # A permission is asked before its call is announced, so this event carries the tool and arguments to draw it.
    tool_name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    command: str = ""
    # Why approval is needed in the client's own words, absent where the reason is prose the harness did not author.
    reason: Optional[PermissionReason] = None
    # Why approval is needed where the text is somebody's prose; the model's own reason lives in its arguments.
    explanation: str = ""


class QuestionEvent(_EventBase):
    kind: Literal["question"] = "question"
    request_id: str
    # The tool call whose ask_user gate this question answers (empty for a bare question).
    tool_call_id: str = ""
    questions: list[dict[str, Any]] = Field(default_factory=list)


class WarningEvent(_EventBase):
    kind: Literal["warning"] = "warning"
    # A non-fatal notice surfaced to the user, as a machine code plus a human title and message.
    code: str = ""
    title: str = ""
    message: str = ""


class ErrorEvent(_EventBase):
    kind: Literal["error"] = "error"
    # A tool-scoped error carries the call id so that card flips to failed; a turn error leaves it empty.
    tool_call_id: str = ""
    tool_name: str = ""
    code: str = "turn_failed"
    title: str = ""
    message: str = ""
    status: int | None = None


# The discriminated union of everything that can appear on the wire.
WireEvent = Annotated[
    Union[
        TextEvent, ThinkingEvent, ThinkingDoneEvent, ToolCallEvent, ToolResultEvent,
        McpEvent, StatusEvent, DoneEvent, CompactionEvent,
        SteeringEvent, TokenUsageEvent, PermissionRequestEvent, QuestionEvent,
        WarningEvent, ErrorEvent,
    ],
    Field(discriminator="kind"),
]

# Every wire-event model, for codegen and for runtime validation dispatch.
WIRE_EVENT_MODELS: tuple[type[_EventBase], ...] = (
    TextEvent, ThinkingEvent, ThinkingDoneEvent, ToolCallEvent, ToolResultEvent,
    McpEvent, StatusEvent, DoneEvent, CompactionEvent,
    SteeringEvent, TokenUsageEvent, PermissionRequestEvent, QuestionEvent,
    WarningEvent, ErrorEvent,
)


# One canonical shape for everything the harness injects into the model's conversation.

class ModelToolResult(BaseModel):
    """The one-line JSON header prepended to every tool result the model reads."""

    kind: Literal["tool_result", "background_result"] = "tool_result"
    tool_name: str
    tool_call_id: str
    status: ToolStatus
    code: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_milliseconds: int | None = None
    background_job_id: str | None = None


class TurnContext(BaseModel):
    """The per-turn context injected at the end of the message list: the time, the place, the goal, the tasks, the work."""

    now: str = ""
    pwd: str = ""
    # The session's goal as the agent wrote it, and where it stands when that is anything other than open.
    goal: dict[str, Any] = Field(default_factory=dict)
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    background: dict[str, Any] = Field(default_factory=dict)
    # Where tools may run and under which mode, here rather than in the cached prompt because it changes mid-session.
    locations: list[dict[str, Any]] = Field(default_factory=list)
    # What the system will permit a tool child and what has been granted on top, which a mid-session grant changes.
    confinement: dict[str, Any] = Field(default_factory=dict)
    # Where a screen script can be pointed and what may be called there, present only when the screen tool is enabled.
    screen: dict[str, Any] = Field(default_factory=dict)


MODEL_ENVELOPE_MODELS: tuple[type[BaseModel], ...] = (
    ModelToolResult, TurnContext, ToolMetadata,
)
