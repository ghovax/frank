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
no import of Frank in the implementer's type. That is what "plug in your own" means concretely,
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
constructs and hands over; `Configuration` is a model `Session` already accepts. Giving
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


# Who decides whether a gated tool call proceeds.


@dataclass(frozen=True)
class SuspensionGate:
    """One human decision a turn is blocked on — a permission prompt or an ``ask_user``
    question.

    ``kind`` discriminates (``"permission"`` | ``"question"``); the permission fields
    (``tool_name``/``arguments``/``command``/``explanation``/``reason``) and the question field
    (``questions``) are populated per kind. A typed carrier so a reader takes ``gate.kind`` rather than an untyped
    ``gate.get("kind")`` off a bare dict.

    It lives here rather than beside the turn events because it is the vocabulary the
    :class:`Approvals` port speaks, and a port whose types live above it could not be
    implemented without reaching into the runtime.

    Its fields are the runtime gate's fields, because one is built from the other's ``to_dict``.
    They are two declarations of one shape and they have drifted before: a field the runtime
    carried and this did not was read off this side anyway, and every turn that raised a
    permission gate died on the attribute that was never here.
    """

    request_id: str = ""
    tool_call_id: str = ""
    kind: str = "permission"
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    command: str = ""
    explanation: str = ""
    # Why approval is needed, as facts rather than prose, so a client writes the sentence in its
    # own language (a ``PermissionReason``, or the plain dict it survives persistence as).
    # ``Any`` because the typed shape belongs to the protocol, which is above this module.
    reason: Any = None
    questions: list[dict[str, Any]] = field(default_factory=list)
    is_bash: bool = False
    deny_message: str = ""
    egress_agent: str = ""
    # The widening a call is asking for, carried so that approving the gate records exactly what
    # was approved rather than re-parsing the arguments to guess at it.
    escape: Any = None
    # Whether approving means "let this one command reach past the workspace", which is what a
    # command the operating system refused is offered.
    whole_disk: bool = False
    denial_evidence: str = ""
    refused_result: Any = None
    grants_screen_mutations: bool = False


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


# Where the audit trail goes.


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


# Where a session's resumable state lives.


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


# Where background jobs are recorded so one survives a restart.


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

    # Keyword-only throughout, and named for what the caller passes. This signature drifted from
    # its only caller once — the protocol said `request=` while `BackgroundJobs.spawn` passed
    # `arguments=` and `tool_call_id=` — and because `JobStore` is `@runtime_checkable`, which
    # compares method *names* and never signatures, nothing caught it at wiring time. It surfaced
    # as a `TypeError` on the first background job of every session, which is every `bash` call.
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
    """Background jobs in a dictionary — the default when nobody supplies one.

    Durable across nothing at all, which is the honest behaviour for a library: a script that
    ends has no restart to survive, and writing a database into the caller's home directory to
    prepare for one is a cost they never asked to pay. The daemon supplies the SQLite store,
    because a daemon is the thing restarts happen to.
    """

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
        # The same keys the SQLite store writes, so a reader cannot tell the two apart. It held
        # `request` here and `arguments_json` there, which would have made every consumer of a job
        # row correct against one store and wrong against the other.
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
    """Where the record of a session's turns goes.

    Deliberately **not** a2a's `TaskStore`, and the difference is the whole reason this exists
    rather than reusing that. The daemon speaks A2A — it is an A2A server, its turns *are*
    Tasks, and its record is rightly a `TaskStore`. The library speaks no A2A at all, so
    handing it Tasks would mean adding a protocol it does not use in order to solve a problem
    it does not have. Adopting an interface is right when the ecosystem has one for the thing
    you are actually doing, and wrong when it merely has one nearby.

    Distinct from :class:`Checkpoints`, which answers "resume this conversation" and holds the
    model-facing messages. This answers "what has this session done", which a program embedding
    an agent wants for auditing, for billing, and for showing a person a history.

    Distinct from :class:`Observer` too: an observation is a decision the harness made
    mid-turn, this is one entry per completed turn.
    """

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
    """Where the OAuth tokens for an account-based provider are kept.

    Only ChatGPT works this way; every other provider takes an API key, which is configuration
    rather than a store. A seam because the alternative is a fixed path under the user's home
    directory, and a program embedding the harness may well hold its tokens somewhere else —
    a secret manager, an encrypted store, its own database.

    Narrow on purpose. Supplying `model=` bypasses this entirely, because a caller who brings
    their own chat model has already solved authentication; this is for a caller who wants
    *our* ChatGPT client with *their* token storage.
    """

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
    """What a compaction strategy is given to decide with.

    Passed rather than reached for. The strategy that ships reads these off the runtime it
    lives on, which is exactly what stops any other strategy existing — a protocol whose
    implementations must know the runtime's attribute names is not a seam.
    """

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
    """Decides when a conversation is folded, and how.

    The default folds older turns into a dense observation log and condenses that log as it
    grows, which costs two model calls and preserves long-horizon memory. That is right for a
    conversation someone returns to and wrong for a scripted agent with a budget, which wants
    the cheap deterministic answer. Neither is the harness's to insist on.
    """

    def should_compact(self, state: CompactionState) -> bool:
        """Whether to fold now. Called before each model call; must be cheap."""
        ...

    async def compact(self, state: CompactionState) -> list:
        """The conversation to carry forward, oldest first."""
        ...


@runtime_checkable
class TurnHook(Protocol):
    """Sees a turn as it runs, and may bound it. It cannot replace the loop.

    Every method is optional; implement only the point you care about. A hook that raises is
    logged and skipped, because a turn must not fail on account of something watching it.

    `before_tools` receives the batch *after* the permission barrier has resolved it, so a
    hook may return fewer calls and never a call the rules denied. A hook narrows; it cannot
    widen. That ordering is what makes bounding a turn expressible as a hook at all.
    """

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
    """Wraps one tool call, the harness's own and the caller's alike.

    `proceed` is the rest of the pipeline, so ordering is explicit at the call site and each
    layer is testable on its own. Without this, a cross-cutting concern — timing, retries,
    caching — can only be built inside a tool, which means a caller's tools can have it and
    the built-ins never can.
    """

    async def run(self, call: Any, proceed: Any) -> Any:
        """Run `call`, or don't, or run it and do something around it."""
        ...


@runtime_checkable
class CatalogueLike(Protocol):
    """The source of everything the prompt is assembled from.

    Five kinds of material — an agent profile, a skill, a memory, an instruction file, a prompt
    template — and they are one port rather than five because they differ in how they are
    *parsed*, not in how they are *found*. Each is named text in a namespace. Six interfaces
    for one concept would be a taxonomy of our filing system rather than a seam.

    The precedent is Jinja2's `Loader`: `FileSystemLoader`, `PackageLoader`, `DictLoader` and
    `FunctionLoader` all behind one `get_source`, which is the most-copied loader design in
    Python and is exactly this problem. Parsing stays ours — a `Skill` is still a `Skill` — so
    an implementation supplies material without having to learn our formats.

    This is a seam because the default is not defensible for a library. Finding this material
    means walking hardcoded paths, and the instruction loader in particular reads
    `~/.config/opencode/AGENTS.md` and `~/.claude/CLAUDE.md` — two *other products'*
    configuration files — out of the user's home directory. That is right for `frankd`, where
    the person running it is the person those files belong to, and indefensible for a harness
    embedded in someone else's program.

    `skills()` and `memories()` are consulted every turn. An implementation therefore decides
    its own reload behaviour by being lazy or not, where the file-backed one re-reads and a
    dictionary-backed one does not — which is the harness getting out of the way of a decision
    that was never its to make.
    """

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
