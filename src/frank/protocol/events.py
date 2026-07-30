"""The single source of truth for streamed events and model-facing envelopes.

Everything the harness streams to a client, and everything it feeds back to the
model, is described here as a Pydantic model. The TypeScript the web client consumes
is *generated* from these models by ``scripts/generate_event_schema.py`` — so the two
sides can never silently drift the way a hand-mirrored ``switch`` used to.

Two design rules make this small instead of sprawling:

* **One vocabulary, tagged by path.** An agent is just an agent running at a
  deeper ``path``; it emits the *same* event kinds as the root agent. There is no
  separate ``sub_task_*`` / ``AGENT_*`` vocabulary and no per-hop re-encoding — a
  parent forwards a child's event by prepending one path segment. ``path == []`` is
  the root agent (the main transcript); any non-empty path renders in the agents
  panel.
* **Lifecycle is a field, not a string suffix.** A tool result carries an explicit
  :class:`ToolStatus`; nothing infers "running" from ``code.endswith("_started")``.
  ``code`` survives only as an optional finer sub-type (e.g. ``cancelled``).

The wire carries the *display* side of a tool result (:attr:`ToolResultEvent.display`)
for the UI; the model reads the same result from the LLM conversation, wrapped in the
:class:`ModelToolResult` header.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, Union

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
    """Correlational + timing facts about a tool call. Shown in the UI and — per the
    product decision — kept visible to the model too, so it can reason about what it
    ran and when."""

    tool_name: str
    tool_call_id: str
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    # Present only for work that was handed to the background runner (bash/search).
    background_job_id: str | None = None


# Streamed events (API -> client)
#
# Every event is a DataPart payload of the shape {kind, ...}, and the client discriminates
# on `kind`. Events carry no tree position: a peer is its own session with its own
# stream, so there is no parent transcript for a child's events to be placed into.

class _EventBase(BaseModel):
    """Base of the wire-event union. Every event contributes its own `kind` literal."""


class TextEvent(_EventBase):
    kind: Literal["text"] = "text"
    text: str


class ThinkingEvent(_EventBase):
    kind: Literal["thinking"] = "thinking"
    text: str = ""
    # The reasoning content-block this chunk belongs to, so the client coalesces a
    # streamed thinking block rather than appending each delta as its own line.
    block_id: str = ""


class ThinkingDoneEvent(_EventBase):
    kind: Literal["thinking_done"] = "thinking_done"
    duration_ms: int = 0


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
    # Finer outcome sub-type when `status` is not enough (e.g. "cancelled" vs a
    # generic error, or a tool-specific success variant). Optional.
    code: str | None = None
    # Whatever the UI should render for this result: arbitrary JSON, shaped by the
    # tool. This is the wire/UI view; the model reads the same result from the LLM
    # conversation, never from here.
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


class CumulativeUsage(BaseModel):
    """Session-lifetime running totals (monotonic), distinct from the per-call figures
    on :class:`TokenUsageEvent` which describe only the latest model call."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
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


class SteeringEvent(_EventBase):
    kind: Literal["steering"] = "steering"
    text: str = ""


class TokenUsageEvent(_EventBase):
    kind: Literal["token_usage"] = "token_usage"
    # Per-call (latest model call) figures — the current context, not a sum.
    input_tokens: int = 0
    output_tokens: int = 0
    context_window: int = 0
    # Session-lifetime running totals for this agent's own calls.
    cumulative: CumulativeUsage = Field(default_factory=CumulativeUsage)


class PermissionRequestEvent(_EventBase):
    kind: Literal["permission_request"] = "permission_request"
    request_id: str
    tool_call_id: str = ""
    # A permission is asked for before its tool call is announced, so this event is the only
    # description of the call a client has when it draws the prompt. It carries the tool and
    # its arguments so the prompt renders as the tool call it is, rather than a bare command.
    tool_name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    command: str = ""
    # Why approval is needed. The model's own reason for wanting the call lives in
    # ``arguments["explanation"]``; both are shown.
    explanation: str = ""
    risk: str = ""


class QuestionEvent(_EventBase):
    kind: Literal["question"] = "question"
    request_id: str
    # The tool call whose ask_user gate this question answers (empty for a bare question).
    tool_call_id: str = ""
    questions: list[dict[str, Any]] = Field(default_factory=list)


class WarningEvent(_EventBase):
    kind: Literal["warning"] = "warning"
    # A non-fatal notice surfaced to the user (e.g. an image attached to a non-vision model):
    # a machine code plus a human title/message. The turn continues.
    code: str = ""
    title: str = ""
    message: str = ""


class ErrorEvent(_EventBase):
    kind: Literal["error"] = "error"
    # A tool-scoped error (an aborted/failed tool call) carries the call id so the UI
    # flips that card to failed; a turn/system error leaves it empty and surfaces a
    # top-level banner from code/title/message.
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


# Model-facing envelopes (harness -> model)
#
# One canonical shape for everything the harness injects into the LLM conversation.

class ModelToolResult(BaseModel):
    """The one-line JSON metadata header prepended to every tool result the model reads —
    inline as a ToolMessage (``kind="tool_result"``) or, for a background completion, as an
    append-only system message (``kind="background_result"``). The tool's raw output body
    follows the header after a blank line, delivered **as-is**: prose stays prose (never
    re-encoded into an escaped JSON string), structured output stays JSON."""

    kind: Literal["tool_result", "background_result"] = "tool_result"
    tool_name: str
    tool_call_id: str
    status: ToolStatus
    code: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    background_job_id: str | None = None


class TurnContext(BaseModel):
    """The structured per-turn context injected at the end of the message list: the current time,
    where the agent is, its goal, its tasks, and its background work."""

    now: str = ""
    pwd: str = ""
    active_goal: str = ""
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    background: dict[str, Any] = Field(default_factory=dict)
    # Where a screen script can be pointed, and what may be called there. Present only when the
    # screen tool is enabled. It is here rather than in the system prompt because the system
    # prompt is cached for the session and windows open and close within one: a cached list of
    # places is a list of places that were open once.
    screen: dict[str, Any] = Field(default_factory=dict)


MODEL_ENVELOPE_MODELS: tuple[type[BaseModel], ...] = (
    ModelToolResult, TurnContext, ToolMetadata,
)
