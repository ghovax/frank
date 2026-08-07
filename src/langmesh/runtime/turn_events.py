"""The typed runtime-to-executor event contract, as a closed union rather than untyped dictionaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Union

from langmesh.base.ports import SuspensionGate
from langmesh.protocol.events import ToolStatus, tool_status_from_result


class EventType(str, Enum):
    STATUS = "status"
    THINKING = "thinking"
    THINKING_DONE = "thinking_done"
    TEXT_CHUNK = "text_chunk"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    MCP_EVENT = "mcp_event"
    USAGE = "usage"
    DONE = "done"
    SUSPENDED = "suspended"
    CHECKPOINT = "checkpoint"
    ERROR = "error"
    DENIED_INJECTION = "denied_injection"
    RETRY_REQUESTED = "retry_requested"
    GROUP_STARTED = "group_started"
    RELAYED = "relayed"
    STEERING = "steering"
    COMPACTION_STARTED = "compaction_started"
    COMPACTION_DONE = "compaction_done"


@dataclass(frozen=True)
class TurnEvent:
    """Base of the closed event union, matched on by variant class rather than by an untyped tag."""

    TYPE: ClassVar[EventType]

    @property
    def type(self) -> EventType:
        return self.TYPE


@dataclass(frozen=True)
class Status(TurnEvent):
    TYPE = EventType.STATUS
    code: str = ""


@dataclass(frozen=True)
class Thinking(TurnEvent):
    TYPE = EventType.THINKING
    text: str = ""
    block_id: str = ""


@dataclass(frozen=True)
class ThinkingDone(TurnEvent):
    TYPE = EventType.THINKING_DONE
    duration_milliseconds: int = 0


@dataclass(frozen=True)
class TextChunk(TurnEvent):
    TYPE = EventType.TEXT_CHUNK
    text: str = ""
    block_id: str = ""


@dataclass(frozen=True)
class ToolCall(TurnEvent):
    TYPE = EventType.TOOL_CALL
    id: str = ""
    name: str = ""
    arguments: Any = None


@dataclass(frozen=True)
class ToolResult(TurnEvent):
    TYPE = EventType.TOOL_RESULT
    id: str = ""
    name: str = ""
    result: Any = None
    status: str = ""
    # Set when this result is a background job's completion, not a synchronous return.
    job_id: str = ""
    # A tool result's payload is genuinely open, so it rides a typed envelope with an open tail.
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # An explicit status wins, else it is derived from the result payload.
        normalized = ToolStatus(self.status or tool_status_from_result(self.result)).value
        object.__setattr__(self, "status", normalized)


@dataclass(frozen=True)
class Mcp(TurnEvent):
    TYPE = EventType.MCP_EVENT
    id: str = ""
    name: str = ""
    server: str = ""
    tool: str = ""
    event: Any = None


@dataclass(frozen=True)
class Usage(TurnEvent):
    TYPE = EventType.USAGE
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    context_window: int = 0
    cumulative: dict[str, Any] = field(default_factory=dict)
    #: Whether every byte shared with the last request was unchanged, which is what makes the read figure readable.
    prefix_intact: bool = False
    #: How much of the prefix was unchanged, estimated with this harness's tokenizer rather than the provider's.
    reachable_tokens: int = 0
    #: How many segments the request had, and how many the previous one already carried unchanged.
    segments: int = 0
    shared_segments: int = 0
    #: The segment that moved, when one did, as fields rather than a sentence.
    divergence: dict[str, Any] | None = None


@dataclass(frozen=True)
class Done(TurnEvent):
    TYPE = EventType.DONE
    text: str = ""
    stop_reason: str = ""


@dataclass(frozen=True)
class Suspended(TurnEvent):
    TYPE = EventType.SUSPENDED
    # The gates awaiting a human, with the plans a resume rebuilds the batch from.
    interactions: list[SuspensionGate] = field(default_factory=list)
    plans: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class Checkpoint(TurnEvent):
    TYPE = EventType.CHECKPOINT


@dataclass(frozen=True)
class Error(TurnEvent):
    TYPE = EventType.ERROR
    message: str = "error"
    id: str = ""
    code: str = ""
    tool: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeniedInjection(TurnEvent):
    TYPE = EventType.DENIED_INJECTION
    command: str = ""
    id: str = ""


@dataclass(frozen=True)
class RetryRequested(TurnEvent):
    """A command the system refused, which somebody could let out of the box; internal to the turn loop."""

    TYPE = EventType.RETRY_REQUESTED
    id: str = ""
    command: str = ""
    denial_kind: str = ""
    denial_evidence: str = ""
    explanation: str = ""
    result: Any = None


@dataclass(frozen=True)
class Steering(TurnEvent):
    TYPE = EventType.STEERING
    text: str = ""
    #: The id the sender gave it, so a client can match this against the message it already has.
    message_id: str = ""
    #: The session that sent it, empty when the person did.
    peer_sender: str = ""


@dataclass(frozen=True)
class CompactionStarted(TurnEvent):
    TYPE = EventType.COMPACTION_STARTED
    reason: str = ""
    messages_before: int = 0
    tokens_before: int = 0


@dataclass(frozen=True)
class CompactionDone(TurnEvent):
    TYPE = EventType.COMPACTION_DONE
    reason: str = ""
    ok: bool = True
    messages_before: int = 0
    messages_after: int = 0
    tokens_before: int = 0
    # What the fold actually reclaimed, reported by every strategy including a supplied one.
    tokens_after: int = 0
    observations_added: int = 0
    directives_added: int = 0
    # How large the memory itself has become, on which the whole schedule of a long session turns.
    log_tokens: int = 0


# The closed union of every turn event, so a consumer can prove exhaustiveness rather than fall through.
TurnEventUnion = Union[
    Status, Thinking, ThinkingDone, TextChunk, ToolCall, ToolResult, Mcp, Usage, Done,
    Suspended, Checkpoint, Error, DeniedInjection, Steering,
    CompactionStarted, CompactionDone,
]
