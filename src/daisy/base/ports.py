"""The seams: what a caller may replace, expressed as interfaces rather than as our classes.

The harness became a library when the runtime stopped keeping session state in module globals.
It became an *embeddable* one here, which is a different property: a runtime you can run twice
in one process is still not something you can put inside your program while every durable thing
it writes goes to a path we chose.

That was not a hypothetical. A `Session` that ran one backgrounded command created a SQLite
database, its write-ahead log, its shared-memory file and a lock file under the caller's XDG
data directory, through a module-level singleton reading a fixed path, with no argument that
could prevent it. The model client could only be built from configuration, so an embedder who
already had a configured `BaseChatModel` could not use it. A gated tool call could only be
answered by consuming a `Suspended` event and calling `resume`, so `ask()` raised rather than
asking. And the conversation checkpoint lived on the daemon's task store, so a library session
could not resume at all.

## The rule

**A seam is an interface, not a class of ours.**

Where the ecosystem already has an interface, we adopt it and make it injectable — LangChain's
`BaseChatModel` for the model, a2a's `TaskStore` for the turn record. Inventing a wrapper around
either would be a second vocabulary for a thing that already has one.

Where it does not, we declare a :class:`typing.Protocol`. `Protocol` is *structural*: an object
satisfies it by having the right methods, with no base class to inherit, no registry to join and
no import of Daisy in the implementer's type. That is what "plug in your own" means concretely,
and it is why this module is deliberately **not** a set of `MemoryX`/`FileX`/`SqliteX` classes.
The deliverable is the shape of the hole. One obvious default is shipped where a default is
needed at all, and the SQLite implementations stay where they belong — in the daemon, which is
the layer that has a database.

Every port is `@runtime_checkable` for exactly one reason: so a near-miss is named at
`Session(...)` rather than surfacing as an `AttributeError` deep in a turn. Structural typing
gives no compile-time guarantee, and a constructor that says *which method is missing* is the
cheapest way to get most of one back.

## Two seams that were already right

`SessionAccess` (how a session reaches its peers) and `LocationExecutor` (where work runs) have
been `Protocol`s taken by injection all along. They are the pattern the rest of this module
follows; nothing here is a new idea, it is an existing one finished.

## What is deliberately not here

No registry, no entry-point discovery, no configuration key naming an implementation by dotted
path. You pass an object. Anything more would be machinery in front of a constructor argument.

And nothing that is already a value. The confinement `Profile` is a frozen dataclass the caller
constructs and hands over; `GlobalConfiguration` is a model `Session` already accepts. Giving
either an interface would be abstraction for its own sake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Mapping, Optional, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - import only for typing; `base` stays free of langchain
    from langchain_core.language_models.chat_models import BaseChatModel

    # The model seam, stated as a type rather than as a Protocol of ours. Every provider this
    # harness routes to already implements it, and so does every mock, tracer and rate limiter
    # in that ecosystem — which is the entire argument for adopting it instead of wrapping it.
    ChatModel = BaseChatModel


# ---------------------------------------------------------------------------------------
# Approvals


@dataclass(frozen=True)
class SuspensionGate:
    """One human decision a turn is blocked on — a permission prompt or an ``ask_user``
    question.

    ``kind`` discriminates (``"permission"`` | ``"question"``); the permission fields
    (``command``/``justification``/``risk``) and the question field (``questions``) are
    populated per kind. A typed carrier so a reader takes ``gate.kind`` rather than an untyped
    ``gate.get("kind")`` off a bare dict.

    It lives here rather than beside the turn events because it is the vocabulary the
    :class:`Approvals` port speaks, and a port whose types live above it could not be
    implemented without reaching into the runtime.
    """

    request_id: str = ""
    tool_call_id: str = ""
    kind: str = "permission"
    command: str = ""
    justification: str = ""
    risk: str = ""
    questions: list[dict[str, Any]] = field(default_factory=list)
    is_bash: bool = False
    deny_message: str = ""
    egress_agent: str = ""


@dataclass(frozen=True)
class Approval:
    """What an approver decided about one gate.

    ``answers`` carries an ``ask_user`` reply and is ignored for a permission gate. A denial
    with a ``reason`` reaches the model as the reason it was denied, which is the difference
    between an agent that adapts and one that retries the same call."""

    allow: bool
    reason: str = ""
    answers: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Approvals(Protocol):
    """Decides whether a gated tool call proceeds, without a human in the loop.

    Absent, a gated turn does what it has always done: emit a suspension and wait to be
    resumed. That is right for a client with a person on the other end and wrong for a script,
    where there is nobody to raise a prompt to and the turn simply hangs on a gate nobody is
    watching.

    Answering ``None`` for a gate means *no opinion* — that gate suspends as before. So an
    implementation can auto-approve the cases it understands and leave the rest to a human,
    rather than facing an all-or-nothing choice.
    """

    async def decide(self, gate: SuspensionGate) -> Optional[Approval]:
        """Decide one gate, or answer ``None`` to leave it to a human."""
        ...


# ---------------------------------------------------------------------------------------
# Observation


@dataclass(frozen=True)
class Observation:
    """Something the harness did that is worth recording but is not a turn event.

    The audit trail: a bash command auto-approved and why, a goal updated, a message added to
    the conversation. These never had anywhere to go — the runtime carried two callbacks for
    them that nothing in the tree ever supplied, so every one of these has been computed and
    discarded since the day it was written.
    """

    session_id: str
    kind: str
    at: datetime
    data: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Observer(Protocol):
    """Receives the audit trail.

    Returning an awaitable is allowed and awaited when the caller is async, which is the
    tolerance `MCPEventCallback` already uses in this tree: a synchronous implementation that
    appends to a list is the common case, and one that writes to a database should not have to
    block the loop to do it.
    """

    def observe(self, observation: Observation) -> Awaitable[None] | None:
        ...


# ---------------------------------------------------------------------------------------
# Checkpoints


@runtime_checkable
class Checkpoints(Protocol):
    """Where a session's resumable state lives: its conversation, and the state around it.

    This is what makes a session survive the process that ran it. In the daemon it is SQLite;
    in a script it is a dictionary; in someone else's program it is whatever they already use,
    which is the point.

    The state is an opaque mapping deliberately. What belongs in a checkpoint is the runtime's
    business and it changes as the runtime does; a port that enumerated the keys would have to
    be revised every time one was added, and would tempt implementations to interpret a
    structure that is not theirs.
    """

    async def save(self, session_id: str, state: Mapping[str, Any]) -> None:
        ...

    async def load(self, session_id: str) -> Optional[Mapping[str, Any]]:
        """The last saved state, or ``None`` for a session that has never been saved."""
        ...


class MemoryCheckpoints:
    """Checkpoints in a dictionary — the default, and the whole of it.

    Shipped because without *some* default a library session cannot resume at all, not as the
    first of a family. It is here rather than as an example in a guide because a default that
    only exists in prose is a default nobody gets.
    """

    def __init__(self) -> None:
        self._states: dict[str, Mapping[str, Any]] = {}

    async def save(self, session_id: str, state: Mapping[str, Any]) -> None:
        self._states[session_id] = dict(state)

    async def load(self, session_id: str) -> Optional[Mapping[str, Any]]:
        return self._states.get(session_id)


# ---------------------------------------------------------------------------------------
# Background jobs


@runtime_checkable
class JobStore(Protocol):
    """Durable record of background jobs, so a long-running task survives a restart.

    Background jobs run as in-process ``asyncio`` tasks, which is fine while the process lives.
    A restart loses any job still running, and — worse — any job that *finished* while the
    model was idle but whose result had not yet been delivered. This is what remembers both.

    It is a port because the alternative was a module-level singleton pointed at a fixed path,
    which meant that embedding the harness in a script wrote a SQLite database into the user's
    data directory on the first backgrounded command, and nothing could stop it.

    The method set is exactly what the runtime and the worker call — no more, so an
    implementation is a day's work, and no less, so one that satisfies this actually works.
    """

    def record_started(
        self, job_id: str, *, session_id: str, agent_name: str, kind: str, request: Mapping[str, Any],
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
    """Background jobs in a dictionary — the default when nobody supplies one.

    Durable across nothing at all, which is the honest behaviour for a library: a script that
    ends has no restart to survive, and writing a database into the caller's home directory to
    prepare for one is a cost they never asked to pay. The daemon supplies the SQLite store,
    because a daemon is the thing restarts happen to.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}

    def record_started(
        self, job_id: str, *, session_id: str, agent_name: str, kind: str, request: Mapping[str, Any],
    ) -> None:
        self._jobs[job_id] = {
            "job_id": job_id,
            "session_id": session_id,
            "agent_name": agent_name,
            "kind": kind,
            "request": dict(request),
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
        # Nothing is ever orphaned here: this store dies with the process that owns the jobs,
        # so there is never a previous run whose children outlived it.
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


def describe_unmet(port: type, candidate: Any) -> str:
    """Which of a port's methods `candidate` is missing, as a sentence, or ``""`` if none.

    `isinstance` against a `runtime_checkable` Protocol answers yes or no, and no is not enough
    to act on — the whole value of checking at the constructor is telling the caller what to
    add. This is what turns a rejection into a fixable one.
    """
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
    "Checkpoints",
    "JobStore",
    "MemoryCheckpoints",
    "MemoryJobStore",
    "Observation",
    "Observer",
    "SuspensionGate",
    "describe_unmet",
]
