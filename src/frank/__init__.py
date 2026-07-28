"""Frank as a library: the harness, without the daemon.

The harness proper — the turn loop, the tools, the prompts, the permission engine, the model
clients — has always been free of the control plane. `runtime` imports nothing from `daemon`
or `worker`; it takes what it needs by injection. What it never had was a front door, so the
only way to run a turn was to start a daemon and drive a session over a socket.

This is that front door.

    import asyncio
    from frank import Session

    async def main() -> None:
        async with Session(agent="general-assistant", directory=".") as session:
            print(await session.ask("what does this project do?"))

    asyncio.run(main())

**Everything durable is a seam.** A library that writes where it likes is a library you cannot
embed, so each thing this one writes down — the conversation checkpoint, the background-job
record, the audit trail — is a constructor argument with an interface behind it, and the
default for each is *nothing on your disk*. Bring your own model, store, approver or observer
by passing an object with the right methods; there is no base class to inherit and no registry
to join. See :mod:`frank.base.ports`.

    from frank import Approval, Session

    class AllowReads:
        async def decide(self, gate):
            if gate.risk in ("", "low"):
                return Approval(allow=True, reason="read-only work is pre-approved")
            return None  # anything else still asks a human

    async with Session("general-assistant", approvals=AllowReads()) as session:
        ...

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

from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Optional, Sequence

from frank.base.catalogue import DictCatalogue
from frank.base.configuration import AgentConfiguration, GlobalConfiguration
from frank.base.permission_mode import PermissionMode
from frank.base.skills import Skill
from frank.runtime.compaction import KeepRecentTurns
from frank.runtime.hooks import MaximumToolCalls
from frank.base.ports import (
    Approval,
    Approvals,
    Catalogue,
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

__all__ = [
    "AgentConfiguration",
    "Approval",
    "Approvals",
    "Catalogue",
    "Checkpoints",
    "Compaction",
    "CompactionState",
    "Credentials",
    "DictCatalogue",
    "GlobalConfiguration",
    "JobStore",
    "KeepRecentTurns",
    "MaximumToolCalls",
    "MemoryCheckpoints",
    "MemoryJobStore",
    "MemoryTranscript",
    "Observation",
    "Observer",
    "PermissionMode",
    "Session",
    "ToolMiddleware",
    "TurnHook",
    "Skill",
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
        catalogue: Optional[Catalogue] = None,
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
        from frank.base.configuration import GlobalConfiguration
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
        # `GlobalConfiguration()`, not `.load()`. A library that reads your home directory
        # because you imported it is not location-agnostic, whatever it does with what it finds.
        # `frank.daemon.machine` is where the XDG loaders live, and it is the daemon's business
        # because the daemon is the program that runs on a machine.
        self._configuration = configuration if configuration is not None else GlobalConfiguration()
        if providers:
            _apply_providers(self._configuration, providers)
        self._model_identifier = model_identifier
        self._model = model
        self._catalogue = _require(Catalogue, catalogue, "catalogue")
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
            from frank.base.confinement import Profile
            from frank.base.tuning import set_tuning, tuning_from_policy
            from frank.runtime.runtime import AgentRuntime

            # The tuning policy is bound per task, so binding it here scopes it to the caller
            # rather than to the interpreter.
            set_tuning(tuning_from_policy(self._configuration.tuning))
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
            catalogue = self._catalogue if self._catalogue is not None else DictCatalogue()
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
                sandbox=self._sandbox if self._sandbox is not None else Profile(),
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

    async def prepare_workspace(self, strategy: str = "worktree") -> str:
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
            from frank.base.workspaces import SessionWorkspaceManager

            manager = SessionWorkspaceManager()
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

    async def stream(self, message: str) -> AsyncIterator[Any]:
        """Drive one turn, yielding each :class:`~frank.runtime.turn_events.TurnEvent`.

        The events are the harness's own vocabulary — text chunks, tool calls, tool results,
        usage, suspensions — the same ones a session streams to a client over its socket.

        The conversation is checkpointed when the turn ends, including when it ends badly: a
        turn that raises has still changed the conversation, and losing that is worse than
        recording a turn that failed."""
        if not self._restored:
            await self.restore()
        try:
            async for event in self.runtime.stream(message):
                yield event
        finally:
            await self.save()

    async def ask(self, message: str) -> str:
        """Drive one turn and answer with the agent's prose.

        A suspension has nowhere to go by default — there is no client to raise a permission
        prompt to — so a turn that needs a human decision raises rather than hanging on a gate
        nobody is watching. Supply `approvals` to answer them in code, choose a permission mode
        the work fits, or drive :meth:`stream` and answer the gates yourself."""
        from frank.runtime.turn_events import Done, Suspended

        answer = ""
        async for event in self.stream(message):
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
