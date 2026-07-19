"""The typed runtime→executor event contract.

A turn's runtime yields events as it runs — text chunks, tool calls and results, usage, the
suspend/checkpoint signals, relayed child events. These used to be one ``StreamEvent(type, **data)``
carrier: an enum tag plus an open, untyped ``**data`` bag that every consumer read back with
``data.get("command", "")``. Adding a field a consumer forgot failed silently, and the contract
between the runtime and the executor lived only in their shared conventions.

This module makes that contract a closed, typed union: one frozen dataclass per event kind, with
its fields named and typed. The executor matches on the variant; an unhandled kind is a real error,
not a dropped ``elif``. Where an event's payload is genuinely open — a tool result carries whatever
the tool returned, an error its detail — the envelope stays typed and only that tail is a dict.

The union is in-process only (it never crosses the A2A wire), so it is plain frozen dataclasses,
not pydantic. Consumers match on the variant class and read typed fields; ``.type`` is available
where the bare :class:`EventType` is convenient.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar

from harness.core.events import ToolStatus, tool_status_from_result


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
    GROUP_STARTED = "group_started"
    RELAYED = "relayed"
    STEERING = "steering"
    COMPACTION_STARTED = "compaction_started"
    COMPACTION_DONE = "compaction_done"


@dataclass(frozen=True)
class TurnEvent:
    """Base of the closed event union. Each variant declares its ``TYPE``; consumers match on the
    variant class (``isinstance``) and read typed fields — there is no untyped ``data`` bag."""

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
    duration_ms: int = 0


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
    task_id: str = ""
    # A tool result's payload is whatever the tool returned — genuinely open, so it rides a typed
    # envelope with an open tail rather than pretending to a fixed shape.
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # An explicit status wins, else it is derived from the result payload.
        normalized = ToolStatus(self.status or tool_status_from_result(self.result)).value
        object.__setattr__(self, "status", normalized)


@dataclass(frozen=True)
class McpEvent(TurnEvent):
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
    agents: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Done(TurnEvent):
    TYPE = EventType.DONE
    text: str = ""
    stop_reason: str = ""


@dataclass(frozen=True)
class Suspended(TurnEvent):
    TYPE = EventType.SUSPENDED
    interactions: list[Any] = field(default_factory=list)
    plans: dict[str, Any] = field(default_factory=dict)


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
class GroupStarted(TurnEvent):
    TYPE = EventType.GROUP_STARTED
    group_id: str = ""
    step_id: str = ""
    agent_name: str = ""
    title: str = ""
    tool_call_id: str = ""


@dataclass(frozen=True)
class Relayed(TurnEvent):
    TYPE = EventType.RELAYED
    event: Any = None


@dataclass(frozen=True)
class Steering(TurnEvent):
    TYPE = EventType.STEERING
    text: str = ""


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
    observations_added: int = 0
