"""Frank as a library: the harness, without the daemon.

The harness proper — the turn loop, the tools, the prompts, the permission engine, the model
clients — has always been free of the control plane. `runtime` imports nothing from `daemon`
or `worker`; it takes what it needs by injection. What it never had was a front door, so the
only way to run a turn was to start a daemon and drive a session over a socket.

This is that front door::

    import asyncio
    from frank import AgentConfiguration, Session

    assistant = AgentConfiguration(
        name="assistant",
        system_prompt="You answer questions about the code in front of you.",
        provider="anthropic",
        model="claude-opus-4-5",
    )

    async def main() -> None:
        async with Session(assistant, directory="/srv/checkout") as session:
            print(await session.ask("what does this project do?"))

    asyncio.run(main())

The agent is the object, not a name for one — a name would mean this library went looking
through your home directory for a profile, which is the thing it exists not to do. The
directory is absolute for the same reason: where tools run is a property of the run, not of
wherever the program happened to be started from.

**Everything durable is a seam.** A library that writes where it likes is a library you cannot
embed, so each thing this one writes down — the conversation checkpoint, the background-job
record, the audit trail — is a constructor argument with an interface behind it, and the
default for each is *nothing on your disk*. Bring your own model, store, approver or observer
by passing an object with the right methods; there is no base class to inherit and no registry
to join. See :mod:`frank.base.ports`::

    from frank import Approval, Session

    class AllowReads:
        async def decide(self, gate):
            if gate.risk in ("", "low"):
                return Approval(allow=True, reason="read-only work is pre-approved")
            return None  # anything else still asks a human

    async def review() -> None:
        async with Session(assistant, directory="/srv/checkout", approvals=AllowReads()) as session:
            print(await session.ask("what changed here recently?"))

**Attachments.** A file is handed over by path, the same act as dragging one into the desktop
app, and it is referenced where it lives rather than copied::

    await session.ask("what is this?", attachments=["~/Downloads/report.pdf"])

The session gains read access to those exact files — and only those — so a path inside a
directory the sandbox otherwise denies still opens. An image is inlined when the model
advertises vision; anything else arrives as a path the agent opens with its file tools.

**What a library session is not.** It is an object in your process, not a process of its own,
so it has none of the three properties the daemon exists to provide: it is not addressable
from outside, it does not outlive the program that made it, and it is not crash-isolated from
you — a tool that exhausts memory takes your process with it. Use `frankd` when you want any of
those. Confinement is unaffected either way, because it was never a property of the session
process: a tool's child is confined at the moment it is spawned, identically here.

**Peers.** `create_session` and its siblings are absent unless you supply `peers`, implementing
:class:`frank.runtime.tools.sessions.SessionAccess`. Composition means addressing another
session, and in a library there is no control plane to address one through. Ask for peers and
you are asking for the daemon.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Optional, Sequence

from frank.base.catalogue import Catalogue
from frank.base.configuration import (
    AgentConfiguration,
    BashToolConfiguration,
    Configuration,
    ToolsConfiguration,
)
from frank.base.permission_mode import PermissionMode
from frank.base.instructions import Instruction
from frank.base.skills import Skill
from frank.runtime.compaction import KeepRecentTurns
from frank.runtime.hooks import MaximumToolCalls
from frank.base.ports import (
    Approval,
    Approvals,
    CatalogueLike,
    Checkpoints,
    Compaction,
    CompactionState,
    Credentials,
    JobStore,
    MemoryCheckpoints,
    MemoryJobStore,
    MemoryTranscript,
    Observation,
    Observer,
    SuspensionGate,
    ToolMiddleware,
    Transcript,
    TurnHook,
    TurnSummary,
    describe_unmet,
)
from frank.base.schedules import ScheduleError, is_due, next_firing, validate as validate_schedule
# The vocabulary `stream()` speaks. Exported because a caller driving a turn has to dispatch on
# these, and reaching into `frank.runtime.turn_events` to name the thing a public method yields
# is the library telling you where its front door is and then handing you the side entrance.
# `TurnEventUnion` is closed, so a `match` over it can prove exhaustiveness with `assert_never`
# and a variant added later becomes a type error rather than a silently dropped branch.
from frank.runtime.turn_events import (
    Checkpoint,
    CompactionDone,
    CompactionStarted,
    Done,
    EventType,
    Mcp,
    Status,
    Steering,
    Suspended,
    TextChunk,
    Thinking,
    ThinkingDone,
    ToolCall,
    ToolResult,
    TurnEvent,
    TurnEventUnion,
    Usage,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AgentConfiguration",
    "BashToolConfiguration",
    "Approval",
    "Approvals",
    "Catalogue",
    "CatalogueLike",
    "Checkpoint",
    "Checkpoints",
    "Compaction",
    "CompactionDone",
    "CompactionStarted",
    "CompactionState",
    "Credentials",
    "Configuration",
    "Done",
    "EventType",
    "Instruction",
    "JobStore",
    "Mcp",
    "KeepRecentTurns",
    "MaximumToolCalls",
    "MemoryCheckpoints",
    "MemoryJobStore",
    "MemoryTranscript",
    "Observation",
    "Observer",
    "PermissionMode",
    "Session",
    "ScheduleError",
    "Status",
    "Steering",
    "Suspended",
    "TextChunk",
    "Thinking",
    "ThinkingDone",
    "ToolCall",
    "ToolMiddleware",
    "ToolResult",
    "TurnEvent",
    "TurnEventUnion",
    "TurnHook",
    "Skill",
    "Usage",
    "is_due",
    "next_firing",
    "validate_schedule",
    "ToolsConfiguration",
    "SuspensionGate",
    "Transcript",
    "TurnSummary",
    "__version__",
]

try:  # pragma: no cover - a source checkout has no distribution metadata
    from importlib.metadata import version as _package_version

    __version__ = _package_version("frank")
except Exception:  # noqa: BLE001 — a missing distribution must not break an import
    __version__ = "0"


def _apply_providers(configuration: Any, providers: Mapping[str, str | Mapping[str, str]]) -> None:
    """Put caller-supplied provider credentials onto a configuration.

    Accepts the short form — ``{"anthropic": "sk-..."}`` — because that is what an embedder
    almost always has, and the long form ``{"custom": {"api_key": ..., "base_url": ...}}`` for
    an OpenAI-compatible endpoint that needs an address too.

    Merged onto the configuration rather than replacing it, so a program can supply one key and
    still inherit the rest of what the machine is set up with, and so the conventional
    environment variables keep the precedence they already have.
    """
    from frank.base.configuration import ProviderCredential

    for name, value in providers.items():
        credential = configuration.providers.get(name) or ProviderCredential()
        if isinstance(value, str):
            credential = credential.model_copy(update={"api_key": value})
        else:
            credential = credential.model_copy(update=dict(value))
        configuration.providers[name] = credential


def _bind_retrieval_policy(configuration: Any) -> None:
    """Bind which models rank a screen, from the loaded configuration.

    Beside :func:`set_tuning` at both call sites rather than inside it: tuning answers "how much
    may a tool return", and which embedding ranks a window is not that question. Importing lazily
    keeps ``frank.computer`` — which pulls in the accessibility stack — off the import path of a
    session that never touches a screen.

    Silent when the screen tool is not configured at all, because a policy for a tool nobody has
    enabled is a load nobody asked for."""
    screen = getattr(configuration, "computer_control", None)
    section = getattr(screen, "retrieval", None)
    if section is None:
        return
    from frank.computer.retrieval import retrieval_policy_from, set_retrieval_policy

    set_retrieval_policy(retrieval_policy_from(section))


def _require(port: type, candidate: Any, argument: str) -> Any:
    """Reject an implementation that does not satisfy its port, naming what is missing.

    Structural typing has no compile-time guarantee, so without this a near-miss surfaces as an
    `AttributeError` several seconds into a turn, from a frame that does not mention the object
    the caller passed. Checking here costs one pass per session and turns that into a sentence
    at the call site."""
    if candidate is None:
        return None
    unmet = describe_unmet(port, candidate)
    if unmet:
        raise TypeError(f"{argument}: {unmet}")
    return candidate


class Session:
    """One agent, driven turn by turn, in this process.

    Built lazily: constructing a session reads configuration and nothing else, so a session
    that is never asked anything costs no model client and no MCP connection. The first turn
    pays for both.
    """

    def __init__(
        self,
        agent: AgentConfiguration,
        *,
        directory: str | Path,
        session_id: str = "",
        permission_mode: str = "",
        sandbox: Any = None,
        configuration: Any = None,
        # Provider credentials in code. A library whose only way to be given an API key is a
        # YAML file in the user's home directory is not a library — and the environment
        # variables still win, so a deployment that injects them keeps doing so.
        providers: Optional[Mapping[str, str | Mapping[str, str]]] = None,
        model_identifier: str = "",
        # The seams. Each defaults to the least surprising thing for a program that is not a
        # daemon, which for anything durable means "in memory", not "somewhere under $HOME".
        model: Any = None,
        catalogue: Optional[CatalogueLike] = None,
        checkpoints: Optional[Checkpoints] = None,
        jobs: Optional[JobStore] = None,
        observer: Optional[Observer] = None,
        approvals: Optional[Approvals] = None,
        transcript: Optional[Transcript] = None,
        credentials: Optional[Credentials] = None,
        peers: Any = None,
        mcp_manager: Any = None,
        # Extension, as distinct from configuration: tools the agent gains, and where it may
        # run them.
        tools: Sequence[Any] = (),
        tool_risk: str = "medium",
        # The three seams around a turn. Each defaults to what the harness has always done,
        # so a caller who passes none of them sees no change.
        hooks: Sequence[Any] = (),
        pipeline: Sequence[Any] = (),
        compaction: Optional[Compaction] = None,
        permissions: Any = None,
        locations: Optional[list[dict]] = None,
        # A git worktree per session. Off by default and deliberately: it writes to disk, and a
        # library that does that unasked is the thing every other default here avoids.
        workspace: Any = None,
        tracer_provider: Any = None,
    ) -> None:
        from frank.base.configuration import Configuration
        from frank.base.identifiers import new_id

        if isinstance(agent, str):
            raise TypeError(
                "agent must be an AgentConfiguration, not a name. A name would mean this "
                "library goes looking for a profile on your machine. Build one in code, or "
                "load it yourself: `frank.daemon.machine.load_catalogue(...).agent(name)`."
            )
        self._agent = agent
        # Absolute, and not resolved against the process's current directory. Where tools run
        # is a property of the run, not of where you happened to start Python.
        if not Path(directory).is_absolute():
            raise ValueError(f"directory must be absolute, got {directory!r}.")
        self._directory = str(Path(directory))
        self._session_id = session_id or new_id("session")
        self._permission_mode = permission_mode
        self._sandbox = sandbox
        self._peers = peers
        self._mcp_manager = mcp_manager
        # `seed=False`: reading configuration must not leave a file in the caller's home
        # directory. The CLI and the daemon seed it deliberately, because a person installing
        # Frank needs something to edit; a program that imported us did not ask for that.
        # `Configuration()`, not `.load()`. A library that reads your home directory
        # because you imported it is not location-agnostic, whatever it does with what it finds.
        # `frank.daemon.machine` is where the XDG loaders live, and it is the daemon's business
        # because the daemon is the program that runs on a machine.
        self._configuration = configuration if configuration is not None else Configuration()
        if providers:
            _apply_providers(self._configuration, providers)
        self._model_identifier = model_identifier
        self._model = model
        self._catalogue = _require(CatalogueLike, catalogue, "catalogue")
        self._checkpoints = _require(Checkpoints, checkpoints, "checkpoints") or MemoryCheckpoints()
        self._jobs = _require(JobStore, jobs, "jobs") or MemoryJobStore()
        self._observer = _require(Observer, observer, "observer")
        self._approvals = _require(Approvals, approvals, "approvals")
        self._transcript = _require(Transcript, transcript, "transcript") or MemoryTranscript()
        self._credentials = _require(Credentials, credentials, "credentials")
        self._tools = list(tools)
        self._tool_risk = tool_risk
        self._permissions = permissions
        self._hooks = list(hooks)
        self._pipeline = list(pipeline)
        self._compaction = _require(Compaction, compaction, "compaction")
        self._locations = locations
        self._workspace = workspace
        self._tracer_provider = tracer_provider
        # Where tools actually run. Equal to `directory` unless a workspace repointed it.
        self._runtime_directory = self._directory
        self._bindings: list = []
        self._runtime: Any = None
        self._restored = False

    @property
    def id(self) -> str:
        """This session's identity, which is what a checkpoint is keyed by."""
        return self._session_id

    @property
    def runtime(self) -> Any:
        """The underlying :class:`~frank.runtime.runtime.AgentRuntime`, built on first use.

        Exposed because a library that hides its own core forces every non-obvious use into a
        fork. Everything below is a convenience over it."""
        if self._runtime is None:
            from frank.base.tuning import set_tuning, tuning_from_policy
            from frank.runtime.runtime import AgentRuntime

            # The tuning policy is bound per task, so binding it here scopes it to the caller
            # rather than to the interpreter.
            set_tuning(tuning_from_policy(self._configuration.tuning))
            _bind_retrieval_policy(self._configuration)
            # Both are bound per task rather than installed on the process, so two sessions in
            # one interpreter can hold different credentials and report to different places.
            # The tokens are held so the bindings end with the session rather than leaking.
            if self._credentials is not None:
                from frank.base.credentials import set_credentials

                self._bindings.append(("credentials", set_credentials(self._credentials)))
            if self._tracer_provider is not None:
                from frank.base.telemetry import set_tracer

                self._bindings.append(
                    ("tracer", set_tracer(self._tracer_provider.get_tracer("frank")))
                )
            # The working directory's own `.agents` plus the packaged base layer, and
            # deliberately nothing of `$HOME`. A program that imported Frank did not ask to
            # inherit the machine's agents, its memories, or — as the instruction loader did —
            # another product's configuration file. `frankd` and the CLI pass a catalogue that
            # does read those, because there the person and the home directory are the same
            # person.
            # An empty catalogue, not a search. Supplying nothing means the session has no
            # skills, no memories and no project instructions — not that the harness should go
            # and find some. Prompt templates still fall back to the packaged ones, which is
            # the library reading itself rather than reading your machine.
            catalogue = self._catalogue if self._catalogue is not None else Catalogue()
            agent_configuration = self._agent
            # A model named at the call site beats the profile's. The common case for an
            # embedder is one agent definition run against whichever model the program is
            # configured for, and editing the profile to say so would be editing a file to
            # express a runtime choice.
            if self._model_identifier:
                if "/" not in self._model_identifier:
                    raise ValueError(
                        f"model_identifier must be `provider/model`, not {self._model_identifier!r}."
                    )
                provider, model = self._model_identifier.split("/", 1)
                agent_configuration = agent_configuration.model_copy(
                    update={"provider": provider, "model": model}
                )
            self._runtime = AgentRuntime(
                agent_configuration=agent_configuration,
                global_configuration=self._configuration,
                session_id=self._session_id,
                working_directory=self._runtime_directory,
                project_directory=self._directory,
                permission_mode=self._permission_mode,
                # Handed over as the caller gave it, including `None`. The runtime normalises
                # every shape in one place, and `None` there means the configured default rather
                # than an empty `Profile()` — which is what this line used to substitute, and
                # which denies every write, in the directory the caller just named as the
                # session's workspace.
                sandbox=self._sandbox,
                session_access=self._peers,
                mcp_manager=self._mcp_manager,
                catalogue=catalogue,
                model=self._model,
                jobs=self._jobs,
                observer=self._observer,
                approvals=self._approvals,
                transcript=self._transcript,
                tools=self._tools,
                tool_risk=self._tool_risk,
                permissions=self._permissions,
                hooks=self._hooks,
                pipeline=self._pipeline,
                compaction=self._compaction,
                locations=self._resolved_locations(),
            )
        return self._runtime

    def _resolved_locations(self) -> Optional[list[dict]]:
        """Where this session's tools may run.

        A caller's own locations win. `None` means the runtime synthesises a single local
        location at the working directory, which is right for a session with no project."""
        return self._locations

    async def prepare_worktree(self, strategy: str = "worktree") -> str:
        """Give this session its own git worktree, and run its tools there.

        Opt-in, and it must be: it writes to disk, where every other default here is chosen so
        that a library session leaves nothing behind. Worth having because an embedder running
        an agent over a repository usually wants exactly this — the agent's edits isolated from
        the working tree the program itself is using.

        Answers with the directory the tools will run in, and repoints the session at it.
        Call before the first turn; a session that has already built its runtime keeps the
        directory it was built with.
        """
        manager = self._workspace
        if manager is None:
            from frank.base.worktrees import SessionWorktreeManager

            manager = SessionWorktreeManager()
        prepared = await manager.prepare(self._session_id, self._directory, strategy)
        self._runtime_directory = prepared.runtime_working_directory or self._directory
        return self._runtime_directory

    @property
    def transcript(self) -> Transcript:
        """The record of this session's turns."""
        return self._transcript

    async def restore(self) -> bool:
        """Reload this session's conversation from its checkpoint store.

        Called automatically before the first turn, so a `Session` given the same id and the
        same store continues where the last one stopped. Answers whether anything was found.
        """
        self._restored = True
        state = await self._checkpoints.load(self._session_id)
        if not state:
            return False
        messages = state.get("conversation") or []
        if not messages:
            return False
        from langchain_core.load import loads as load_message

        self.runtime.conversation[:] = [load_message(entry) for entry in messages]
        return True

    async def save(self) -> None:
        """Write this session's conversation to its checkpoint store.

        Serialised through LangChain's own codec rather than by hand: a message is not a flat
        dictionary — it carries tool calls, content blocks and provider-specific metadata — and
        a checkpoint that dropped any of those would restore a conversation the provider
        rejects."""
        from langchain_core.load import dumps as dump_message

        await self._checkpoints.save(
            self._session_id,
            {"conversation": [dump_message(message) for message in self.conversation]},
        )

    def _compose(self, message: str, attachments: Sequence[str | Path]) -> object:
        """The model-facing input for a turn, including the files the caller attached.

        The same composition the daemon performs for a message from the desktop app, reached
        through the same function — so a program embedding this harness attaches a file the
        way its own client does, rather than through a shape it had to reverse-engineer.

        The attached paths are also recorded on the runtime, which is what makes them readable
        at all: `~/Downloads`, `~/Desktop` and `~/Documents` are denied to a confined tool
        child, and a path the model cannot open is not an attachment.
        """
        if not attachments:
            return message
        from frank.protocol.files import attachment_from_path
        from frank.protocol.parts import attachment_payload, compose_turn_input

        records = [attachment_from_path(path) for path in attachments]
        runtime = self.runtime
        runtime.note_attachments([record["path"] for record in records])
        turn_input, images_not_inlined = compose_turn_input(
            message, [attachment_payload(records)], runtime.effective_model_identifier,
        )
        if images_not_inlined:
            # The daemon raises a warning event to its client; a library caller may have no
            # client to raise one to, so this goes to the log it does have. Silence would be
            # worse: the model gets the path and not the pixels, and nothing would say why.
            logger.warning(
                "%d attached image(s) were not inlined: %s does not advertise vision support. "
                "The model has the file paths and can open them with its tools.",
                images_not_inlined, runtime.effective_model_identifier or "the session model",
            )
        return turn_input

    async def stream(
        self, message: str, *, attachments: Sequence[str | Path] = (),
    ) -> AsyncIterator[TurnEventUnion]:
        """Drive a turn, yielding each :class:`TurnEvent`.

        One turn, unless the agent sets a goal. A goal is a contract for an outcome that
        outlives a single turn, so while one is open this keeps driving turns toward it and
        keeps yielding their events — bounded by ``Tunable.goal_continuation_turns``, and ended
        the moment the agent satisfies, clears or reports it blocked. Calling again gives the
        allowance back, because being spoken to is what it was waiting for.

        ``attachments`` are local file paths the caller is handing to the agent, exactly as a
        person dragging a file into the desktop app would::

            async for event in session.stream("what is this?", attachments=["~/Downloads/report.pdf"]):
                ...

        Each file is referenced where it lives — nothing is copied — and the session gains
        read access to those exact files for the rest of its life. An image is inlined when
        the model advertises vision; anything else reaches the model as a path it opens with
        its file tools. A path that is not a regular file raises ``FileNotFoundError``.

        The events are the harness's own vocabulary — text chunks, tool calls, tool results,
        usage, compaction, suspensions — the same ones a session streams to a client over its
        socket, and every one of them is exported from ``frank``. The union is closed, so a
        ``match`` over it can prove exhaustiveness with ``assert_never``.

        Folding announces itself here like anything else, which is how a program watches its
        own memory being rewritten::

            async for event in session.stream("keep going"):
                match event:
                    case CompactionStarted(tokens_before=before):
                        log.info("folding at %d tokens", before)
                    case CompactionDone(ok=True, messages_before=b, messages_after=a):
                        log.info("folded %d messages into %d", b, a)
                    case Usage(cumulative=totals):
                        meter.record(totals)

        The conversation is checkpointed when the turn ends, including when it ends badly: a
        turn that raises has still changed the conversation, and losing that is worse than
        recording a turn that failed."""
        if not self._restored:
            await self.restore()
        # Somebody is here again, so a goal that had used its allowance gets it back and is
        # picked up where it stopped.
        self.runtime.restore_goal_allowance()
        try:
            async for event in self.runtime.stream(self._compose(message, attachments)):
                yield event
            # A goal outlives the turn that set it, so this call is over when the *goal* is,
            # not when the model first stops talking. Each further pass is a real turn — the
            # same events, through the same loop — opened with the goal restated, and the whole
            # stretch is bounded by `Tunable.goal_continuation_turns` so a program that asks one
            # question cannot be handed an unbounded run. Without this, a library session could
            # set a goal and nothing would ever act on it: in the desktop app the layer above
            # opens those turns, and here there is no layer above.
            async for event in self._pursue_goal():
                yield event
        finally:
            await self.save()

    async def _pursue_goal(self) -> AsyncIterator[TurnEventUnion]:
        """Keep driving turns while the session's goal is open, up to its allowance."""
        from frank.base.tuning import Tunable, active_tuning

        allowance = active_tuning().amount(Tunable.goal_continuation_turns)
        while True:
            goal = self.runtime.goal
            if goal is None or not goal.is_open:
                return
            if goal.continuations >= allowance:
                self.runtime.park_goal()
                return
            self.runtime.note_goal_continuation()
            async for event in self.runtime.stream(self._goal_continuation_note(goal), as_system_note=True):
                yield event

    def _goal_continuation_note(self, goal) -> str:
        """The goal, restated as the next turn's opening message."""
        from frank.base.tuning import Tunable, active_tuning

        return self.runtime._prompt_loader.load("goal_continuation", {
            "goal": goal.text,
            "requirements": "\n".join(f"- {requirement}" for requirement in goal.requirements),
            "blocked_turns": active_tuning().amount(Tunable.goal_blocked_turns),
        })

    async def ask(self, message: str, *, attachments: Sequence[str | Path] = ()) -> str:
        """Drive a turn — or a goal to its end, as :meth:`stream` describes — and answer with
        the agent's prose.

        ``attachments`` are local file paths handed to the agent, as in :meth:`stream`.

        A suspension has nowhere to go by default — there is no client to raise a permission
        prompt to — so a turn that needs a human decision raises rather than hanging on a gate
        nobody is watching. Supply `approvals` to answer them in code, choose a permission mode
        the work fits, or drive :meth:`stream` and answer the gates yourself."""
        from frank.runtime.turn_events import Done, Suspended

        answer = ""
        async for event in self.stream(message, attachments=attachments):
            if isinstance(event, Suspended):
                raise PermissionError(
                    "This turn needs a human decision, and nothing is answering gates. Pass "
                    "`approvals=` to decide them in code, drive `stream()` and answer them "
                    "yourself, or create the session in a permission mode that does not gate "
                    "this work."
                )
            if isinstance(event, Done):
                answer = event.text or answer
        return answer

    async def compact(self) -> AsyncIterator[TurnEventUnion]:
        """Fold the conversation now, yielding the same events an automatic fold does.

        The manual counterpart to the automatic trigger, and the same call the desktop's Compact
        button makes through the daemon. It runs a pass regardless of how full the context is —
        that is what makes it manual — so a program that meters its own spend can fold on its own
        terms rather than waiting for the threshold, and one that has just finished a noisy phase
        can put it behind itself before starting the next.

        A pass with nothing to fold yields nothing and changes nothing, so calling this
        speculatively is safe. The conversation is checkpointed afterwards, because a fold is a
        change to it and losing that would leave the store describing a conversation that no
        longer exists::

            async for event in session.compact():
                match event:
                    case CompactionDone(ok=False):
                        log.warning("nothing was folded; history is untouched")
        """
        if not self._restored:
            await self.restore()
        try:
            async for event in self.runtime.compact(reason="manual"):
                yield event
        finally:
            await self.save()

    @property
    def conversation(self) -> list:
        """The model-facing message list, which accumulates across turns."""
        return self.runtime.conversation

    async def aclose(self) -> None:
        """Release what the session opened: background jobs, and the browser if it was used."""
        import contextlib
        import sys

        if self._runtime is not None:
            with contextlib.suppress(Exception):
                self._runtime.abort()
        # Unbind what the session bound, so a caller's credentials and tracer do not outlive
        # the session that supplied them.
        for kind, token in reversed(self._bindings):
            with contextlib.suppress(Exception):
                if kind == "credentials":
                    from frank.base.credentials import reset_credentials

                    reset_credentials(token)
                else:
                    from frank.base.telemetry import reset_tracer

                    reset_tracer(token)
        self._bindings.clear()
        from frank.runtime.background import cancel_all_background_jobs

        with contextlib.suppress(Exception):
            cancel_all_background_jobs()
        if "frank.computer.web" in sys.modules:
            with contextlib.suppress(Exception):
                sys.modules["frank.computer.web"].close()

    async def __aenter__(self) -> "Session":
        return self

    async def __aexit__(self, *_exception: object) -> None:
        await self.aclose()
