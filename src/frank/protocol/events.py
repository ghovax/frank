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

    #: When this event was made, ISO-8601 in UTC.
    #
    #: On every event rather than on the few that seemed to want one, because which events want
    #: one is not knowable in advance: the transcript's own order answers "what happened next"
    #: but never "how long after", and that second question is the one asked of stored data
    #: afterwards. Two calls seconds apart and two calls minutes apart are indistinguishable in
    #: an append-only log, and the difference between them decides whether a prompt cache was
    #: still warm — which could not be checked at all until this existed.
    #
    #: Stamped when the event is constructed, which is where the thing being described happened;
    #: a time added on the way to disk would measure the writer, not the event.
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


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


class TracedSegment(BaseModel):
    """Which piece of a request a cache measurement is talking about.

    Fields rather than a formatted label, so a consumer can count how often the tool schemas
    move or which role tends to be rewritten. ``position`` is the index within the conversation,
    or ``-1`` for the parts a request has only one of.
    """

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
    """Session-lifetime running totals (monotonic), distinct from the per-call figures
    on :class:`TokenUsageEvent` which describe only the latest model call."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    #: What a cache could have returned across the session, so `cache_read_tokens` has a
    #: denominator that means something. Against total input a perfect cache still reads about
    #: 70%, because every token is paid for once before it can ever be served from cache.
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
    #: The session that sent this, when it was not the person. A message arriving mid-turn is
    #: injected into the running turn rather than starting a new one — and that path had no way
    #: to say who it came from, so a peer's report and the daemon's own notice about a dead child
    #: reached the transcript wearing the user's name. The same distinction the turn kind makes
    #: for a message that starts a turn, made for one that joins a turn already running.
    peer_sender: str = ""
    #: The id the sender gave this message, carried back so a client can recognise the message
    #: it already knows about. Without it the only handle was the text, and a client that had
    #: shown the message optimistically could not tell its own copy from the one the session
    #: persisted — so both were on screen until a replay rebuilt the list and dropped one.
    message_id: str = ""


class TokenUsageEvent(_EventBase):
    kind: Literal["token_usage"] = "token_usage"
    # Per-call (latest model call) figures — the current context, not a sum.
    input_tokens: int = 0
    output_tokens: int = 0
    context_window: int = 0
    # Per-call cache and reasoning, which used to be folded into the cumulative totals and
    # nowhere else. A running total cannot say which call missed, and "which call missed" is
    # the whole question — a session reading 2% overall turned out to be one partial hit and
    # five outright misses, which the cumulative figure hid completely.
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    # Session-lifetime running totals for this agent's own calls.
    cumulative: CumulativeUsage = Field(default_factory=CumulativeUsage)
    # What the cache figure means, which the figure alone cannot say. A provider serves the
    # longest prefix it recognises, so a low read is either a prefix that moved — and
    # ``divergence`` says which piece — or an unchanged prefix the provider missed anyway,
    # which is ``prefix_intact`` with a read of zero and is not something a differently shaped
    # request would cure. ``reachable_tokens`` is the ceiling the read is measured against,
    # estimated with this harness's tokenizer rather than the provider's.
    #
    # Recorded on the event rather than logged: this is asked about days later, of a specific
    # call in a specific session, and only stored data can answer that.
    prefix_intact: bool = False
    reachable_tokens: int = 0
    segments: int = 0
    shared_segments: int = 0
    divergence: Optional[PrefixDivergence] = None


class PermissionReason(BaseModel):
    """Why approval is needed, as data rather than as a sentence.

    The harness used to build the sentence itself — "Sandbox approval required: this command
    reads outside the working directory (/a, /b)." — and hand a client the finished English.
    That put user-facing prose in the one place that cannot translate it: the daemon has no
    locale, the string never reached the message catalogue, and a Japanese interface rendered
    an English clause with a colon and a parenthetical in the middle of its own layout.

    So the harness states the *facts* and the client writes the sentence. `kind` selects the
    message; the paths ride as data the message interpolates. A reason the client does not
    recognise falls back to the model's own explanation, which is prose either way."""

    kind: str
    paths: list[str] = Field(default_factory=list)


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
    # Why approval is needed, in the client's own words. Absent where the reason is prose the
    # harness did not author — the reviewer's verdict, or the model's own account of itself.
    reason: Optional[PermissionReason] = None
    # Why approval is needed, where the text is somebody's prose rather than a fact about the
    # call. The model's own reason for wanting the call lives in ``arguments["explanation"]``;
    # both are shown.
    explanation: str = ""


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
    # The session's goal as the agent wrote it: the end state and the conditions that would
    # prove it, plus where it stands when that is anything other than open. What is counted in
    # order to keep the session working is not here — see `frank.runtime.goal`.
    goal: dict[str, Any] = Field(default_factory=dict)
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    background: dict[str, Any] = Field(default_factory=dict)
    # Where tools may run, and under which permission mode. Here because the mode is changeable
    # while a session runs: stating it in the cached prompt meant every change rewrote the front
    # of the request and threw away the cache for the whole conversation.
    #
    # The machine snapshot is deliberately NOT here. It is minted once, at the session's first
    # message, and appended to the conversation — see `_environment_note`. A snapshot of a
    # machine does not need restating every turn, and restating it would cost its own size on
    # each one; appended once, it is cached with everything else from the second call onward.
    locations: list[dict[str, Any]] = Field(default_factory=list)
    # What the operating system will actually permit a tool child, and what has been granted on
    # top of it. Here, and not in the system prompt, for the reason `locations` is here: a grant
    # approved mid-session changes it, and anything changeable in the cached prefix rewrites the
    # front of every request.
    #
    # It is here *at all* because it was nowhere. A session was told its permission mode and
    # never its confinement, so the first it learned of the boundary was an `Operation not
    # permitted` that named no path — a boundary you can only discover by hitting it is one you
    # hit repeatedly.
    confinement: dict[str, Any] = Field(default_factory=dict)
    # Where a screen script can be pointed, and what may be called there. Present only when the
    # screen tool is enabled. It is here rather than in the system prompt because the system
    # prompt is cached for the session and windows open and close within one: a cached list of
    # places is a list of places that were open once.
    screen: dict[str, Any] = Field(default_factory=dict)


MODEL_ENVELOPE_MODELS: tuple[type[BaseModel], ...] = (
    ModelToolResult, TurnContext, ToolMetadata,
)
