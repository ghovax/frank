"""The seams: what a caller may replace, expressed as interfaces rather than as our classes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Mapping, Optional, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - import only for typing; `base` stays free of langchain
    from langchain_core.language_models.chat_models import BaseChatModel

    # The model seam, stated as a type rather than as a Protocol of ours.
    ChatModel = BaseChatModel


# Who decides whether a gated tool call proceeds.


@dataclass(frozen=True)
class SuspensionGate:
    """One human decision a turn is blocked on — a permission prompt or an ``ask_user`` question."""

    request_id: str = ""
    tool_call_id: str = ""
    kind: str = "permission"
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    command: str = ""
    explanation: str = ""
    # Why approval is needed, as facts rather than prose, so a client writes the sentence in its own language (a ``PermissionReason``, or the plain dict it survives persistence as).
    reason: Any = None
    questions: list[dict[str, Any]] = field(default_factory=list)
    is_bash: bool = False
    deny_message: str = ""
    egress_agent: str = ""
    # The widening a call is asking for, carried so that approving the gate records exactly what was approved rather than re-parsing the arguments to guess at it.
    escape: Any = None
    # Whether approving means "let this one command reach past the workspace", which is what a command the operating system refused is offered.
    whole_disk: bool = False
    denial_evidence: str = ""
    refused_result: Any = None
    grants_screen_mutations: bool = False


@dataclass(frozen=True)
class Approval:
    """What an approver decided about one gate."""

    allow: bool
    reason: str = ""
    answers: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Approvals(Protocol):
    """Decides whether a gated tool call proceeds, without a human in the loop."""

    async def decide(self, gate: SuspensionGate) -> Optional[Approval]:
        """Decide one gate, or answer ``None`` to leave it to a human."""
        ...


# Where the audit trail goes.


@dataclass(frozen=True)
class Observation:
    """Something the harness did that is worth recording but is not a turn event."""

    session_id: str
    kind: str
    at: datetime
    data: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Observer(Protocol):
    """Receives the audit trail."""

    def observe(self, observation: Observation) -> Awaitable[None] | None:
        ...


# Where a session's resumable state lives.


@runtime_checkable
class Checkpoints(Protocol):
    """Where a session's resumable state lives: its conversation, and the state around it."""

    async def save(self, session_id: str, state: Mapping[str, Any]) -> None:
        ...

    async def load(self, session_id: str) -> Optional[Mapping[str, Any]]:
        """The last saved state, or ``None`` for a session that has never been saved."""
        ...


class MemoryCheckpoints:
    """Checkpoints in a dictionary — the default, and the whole of it."""

    def __init__(self) -> None:
        self._states: dict[str, Mapping[str, Any]] = {}

    async def save(self, session_id: str, state: Mapping[str, Any]) -> None:
        self._states[session_id] = dict(state)

    async def load(self, session_id: str) -> Optional[Mapping[str, Any]]:
        return self._states.get(session_id)


# Where background jobs are recorded so one survives a restart.


@runtime_checkable
class JobStore(Protocol):
    """Durable record of background jobs, so a long-running task survives a restart."""

    # Keyword-only throughout, and named for what the caller passes.
    def record_started(
        self,
        *,
        job_id: str,
        session_id: str,
        agent_name: str,
        kind: str,
        arguments: Mapping[str, Any],
        tool_call_id: str = "",
    ) -> None: ...

    def record_process_group(self, job_id: str, process_group: int) -> None: ...

    def record_finished(self, job_id: str, result: str, *, status: str = ...) -> None: ...

    def mark_delivered(self, job_id: str) -> None: ...

    def mark_abandoned(self, job_id: str, result: str) -> None: ...

    def running_jobs(self, agent_name: str | None = None) -> Sequence[Mapping[str, Any]]: ...

    def orphaned_process_groups(self) -> Sequence[int]: ...

    def undelivered_jobs(self, session_id: str, agent_name: str) -> Sequence[Mapping[str, Any]]: ...

    def has_undelivered_jobs(self, session_id: str, agent_name: str) -> bool: ...

    def contexts_with_undelivered(self, agent_name: str) -> Sequence[str]: ...


class MemoryJobStore:
    """Background jobs in a dictionary — the default when nobody supplies one."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}

    def record_started(
        self,
        *,
        job_id: str,
        session_id: str,
        agent_name: str,
        kind: str,
        arguments: Mapping[str, Any],
        tool_call_id: str = "",
    ) -> None:
        # The same keys the SQLite store writes, so a reader cannot tell the two apart.
        self._jobs[job_id] = {
            "job_id": job_id,
            "session_id": session_id,
            "agent_name": agent_name,
            "kind": kind,
            "arguments": dict(arguments),
            "tool_call_id": tool_call_id,
            "status": "running",
            "result": "",
            "process_group": 0,
        }

    def record_process_group(self, job_id: str, process_group: int) -> None:
        if job_id in self._jobs:
            self._jobs[job_id]["process_group"] = process_group

    def record_finished(self, job_id: str, result: str, *, status: str = "completed") -> None:
        if job_id in self._jobs:
            self._jobs[job_id].update(result=result, status=status)

    def mark_delivered(self, job_id: str) -> None:
        if job_id in self._jobs:
            self._jobs[job_id]["status"] = "delivered"

    def mark_abandoned(self, job_id: str, result: str) -> None:
        if job_id in self._jobs:
            self._jobs[job_id].update(result=result, status="abandoned")

    def running_jobs(self, agent_name: str | None = None) -> Sequence[Mapping[str, Any]]:
        return [
            job for job in self._jobs.values()
            if job["status"] == "running" and (agent_name is None or job["agent_name"] == agent_name)
        ]

    def orphaned_process_groups(self) -> Sequence[int]:
        # Nothing is ever orphaned here: this store dies with the process that owns the jobs, so there is never a previous run whose children outlived it.
        return []

    def undelivered_jobs(self, session_id: str, agent_name: str) -> Sequence[Mapping[str, Any]]:
        return [
            job for job in self._jobs.values()
            if job["status"] == "completed"
            and job["session_id"] == session_id
            and job["agent_name"] == agent_name
        ]

    def has_undelivered_jobs(self, session_id: str, agent_name: str) -> bool:
        return bool(self.undelivered_jobs(session_id, agent_name))

    def contexts_with_undelivered(self, agent_name: str) -> Sequence[str]:
        return sorted({
            job["session_id"] for job in self._jobs.values()
            if job["status"] == "completed" and job["agent_name"] == agent_name
        })


# The record of what a session actually did.


@dataclass(frozen=True)
class TurnSummary:
    """One completed turn: what was asked, what came back, and how it ended."""

    session_id: str
    turn_id: str
    started_at: datetime
    ended_at: datetime
    request: str
    response: str
    # "completed" | "cancelled" | "failed" | "input_required" — the runtime's own stop reason.
    outcome: str
    tools_called: Sequence[str] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    error: str = ""


@runtime_checkable
class Transcript(Protocol):
    """Where the record of a session's turns goes."""

    async def record(self, turn: TurnSummary) -> None:
        ...

    async def turns(self, session_id: str) -> Sequence[TurnSummary]:
        """Every turn recorded for a session, oldest first."""
        ...


class MemoryTranscript:
    """Turns in a list. The default, and the whole of it."""

    def __init__(self) -> None:
        self._turns: dict[str, list[TurnSummary]] = {}

    async def record(self, turn: TurnSummary) -> None:
        self._turns.setdefault(turn.session_id, []).append(turn)

    async def turns(self, session_id: str) -> Sequence[TurnSummary]:
        return list(self._turns.get(session_id, ()))


# The account credentials a provider needs, when it uses an account rather than a key.


@runtime_checkable
class Credentials(Protocol):
    """Where the OAuth tokens for an account-based provider are kept."""

    def load(self) -> Any:
        """The stored tokens, or ``None`` when nothing is signed in."""
        ...

    def save(self, tokens: Any) -> None:
        ...

    def clear(self) -> None:
        ...


# Where the prompt's material comes from.


@dataclass
class CompactionState:
    """What a compaction strategy is given to decide with."""

    messages: list
    """The conversation as it stands, oldest first."""

    context_window: int
    """The live model's context window in tokens, or 0 when it is not known."""

    context_tokens: int
    """What the conversation currently occupies, as the last reply reported it."""

    reason: str = "auto"
    """``auto`` when the loop asked, ``manual`` when a person did."""


@runtime_checkable
class Compaction(Protocol):
    """Decides when a conversation is folded, and how."""

    def should_compact(self, state: CompactionState) -> bool:
        """Whether to fold now. Called before each model call; must be cheap."""
        ...

    async def compact(self, state: CompactionState) -> list:
        """The conversation to carry forward, oldest first."""
        ...


@runtime_checkable
class TurnHook(Protocol):
    """Sees a turn as it runs, and may bound it. It cannot replace the loop."""

    async def before_model(self, messages: list) -> list:
        """The conversation about to go to the model. Return it, or a changed copy."""
        ...

    async def before_tools(self, calls: list[dict]) -> list[dict]:
        """The approved batch about to run. Return it, a subset, or an empty list."""
        ...

    async def after_turn(self, summary: TurnSummary) -> None:
        """The turn is over: what it did, what it cost, how it ended."""
        ...


@runtime_checkable
class ToolMiddleware(Protocol):
    """Wraps one tool call, the harness's own and the caller's alike."""

    async def run(self, call: Any, proceed: Any) -> Any:
        """Run `call`, or don't, or run it and do something around it."""
        ...


@runtime_checkable
class CatalogueLike(Protocol):
    """The source of everything the prompt is assembled from."""

    def agent(self, name: str) -> Any:
        """The named agent profile, or ``None`` if this catalogue has no such agent."""
        ...

    def agents(self) -> Sequence[str]:
        """Every agent name this catalogue can supply, for listing and for error messages."""
        ...

    def skills(self) -> Sequence[Any]:
        ...

    def memories(self) -> Sequence[Any]:
        ...

    def instructions(self) -> Sequence[Any]:
        """The project's own conventions, as `Instruction` values."""
        ...

    def prompt(self, name: str, variables: Mapping[str, str]) -> str:
        """One rendered prompt template, or ``""`` when this catalogue has no such template."""
        ...


def describe_unmet(port: type, candidate: Any) -> str:
    """Which of a port's methods `candidate` is missing, as a sentence, or ``""`` if none."""
    missing = sorted(
        name for name in getattr(port, "__protocol_attrs__", ())
        if not hasattr(candidate, name)
    )
    if not missing:
        return ""
    described = ", ".join(f"`{name}`" for name in missing)
    return (
        f"{type(candidate).__name__} does not satisfy {port.__name__}: it is missing "
        f"{described}."
    )


__all__ = [
    "Approval",
    "Approvals",
    "CatalogueLike",
    "Checkpoints",
    "Credentials",
    "JobStore",
    "MemoryCheckpoints",
    "MemoryJobStore",
    "MemoryTranscript",
    "Observation",
    "Observer",
    "Compaction",
    "CompactionState",
    "SuspensionGate",
    "ToolMiddleware",
    "TurnHook",
    "Transcript",
    "TurnSummary",
    "describe_unmet",
]
