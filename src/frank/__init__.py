"""Frank as a library: the harness without the daemon, driven turn by turn through `Session`."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Optional, Sequence

from frank.base.catalogue import Catalogue
from frank.base.configuration import (
    AgentConfiguration,
    BashToolConfiguration,
    Configuration,
    FilesystemConfiguration,
    SandboxConfiguration,
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
# The vocabulary `stream()` speaks, exported because a caller driving a turn has to dispatch on it.
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
    "FilesystemConfiguration",
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
    "SandboxConfiguration",
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
    """Put caller-supplied provider credentials onto a configuration, accepting the short form as well as the long."""
    from frank.base.configuration import ProviderCredential

    for name, value in providers.items():
        credential = configuration.providers.get(name) or ProviderCredential()
        if isinstance(value, str):
            credential = credential.model_copy(update={"api_key": value})
        else:
            credential = credential.model_copy(update=dict(value))
        configuration.providers[name] = credential


def _bind_retrieval_policy(configuration: Any) -> None:
    """Bind which models rank a screen, from the loaded configuration, beside the tuning policy rather than inside it."""
    screen = getattr(configuration, "computer_control", None)
    section = getattr(screen, "retrieval", None)
    if section is None:
        return
    from frank.computer.retrieval import retrieval_policy_from, set_retrieval_policy

    set_retrieval_policy(retrieval_policy_from(section))


def _require(port: type, candidate: Any, argument: str) -> Any:
    """Reject an implementation that does not satisfy its port, naming what is missing rather than failing mid-turn."""
    if candidate is None:
        return None
    unmet = describe_unmet(port, candidate)
    if unmet:
        raise TypeError(f"{argument}: {unmet}")
    return candidate


class Session:
    """One agent driven turn by turn in this process, built lazily so an unused session costs nothing."""

    def __init__(
        self,
        agent: AgentConfiguration,
        *,
        directory: str | Path,
        session_id: str = "",
        permission_mode: str = "",
        sandbox: Any = None,
        configuration: Any = None,
        # Provider credentials in code, though the environment variables still win.
        providers: Optional[Mapping[str, str | Mapping[str, str]]] = None,
        model_identifier: str = "",
        # The seams, each defaulting to the least surprising thing for a program that is not a daemon.
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
        # Extension as distinct from configuration: tools the agent gains, and where it may run them.
        tools: Sequence[Any] = (),
        supplied_tool_gate: str = "ask",
        # The three seams around a turn, each defaulting to what the harness has always done.
        hooks: Sequence[Any] = (),
        pipeline: Sequence[Any] = (),
        compaction: Optional[Compaction] = None,
        permissions: Any = None,
        locations: Optional[list[dict]] = None,
        # A git worktree per session, off by default because it writes to disk.
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
        # Absolute, and not resolved against the process's directory: where tools run is a property of the run.
        if not Path(directory).is_absolute():
            raise ValueError(f"directory must be absolute, got {directory!r}.")
        self._directory = str(Path(directory))
        self._session_id = session_id or new_id("session")
        self._permission_mode = permission_mode
        self._sandbox = sandbox
        self._peers = peers
        self._mcp_manager = mcp_manager
        # Reading configuration must not leave a file in the caller's home directory.
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
        self._supplied_tool_gate = supplied_tool_gate
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
        """The underlying `AgentRuntime`, built on first use and exposed so no non-obvious use needs a fork."""
        if self._runtime is None:
            from frank.base.tuning import set_tuning, tuning_from_policy
            from frank.runtime.runtime import AgentRuntime

            # The tuning policy is bound per task, so binding it here scopes it to the caller rather than the interpreter.
            set_tuning(tuning_from_policy(self._configuration.tuning))
            _bind_retrieval_policy(self._configuration)
            # Both are bound per task, so two sessions in one interpreter can hold different credentials and tracers.
            if self._credentials is not None:
                from frank.base.credentials import set_credentials

                self._bindings.append(("credentials", set_credentials(self._credentials)))
            if self._tracer_provider is not None:
                from frank.base.telemetry import set_tracer

                self._bindings.append(
                    ("tracer", set_tracer(self._tracer_provider.get_tracer("frank")))
                )
            # The working directory's own `.agents` plus the packaged base layer, and deliberately nothing of `$HOME`.
            catalogue = self._catalogue if self._catalogue is not None else Catalogue()
            agent_configuration = self._agent
            # A model named at the call site beats the profile's, since editing a file to express a runtime choice is the wrong seam.
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
                # Handed over as the caller gave it, including `None`, which the runtime reads as the configured default.
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
                supplied_tool_gate=self._supplied_tool_gate,
                permissions=self._permissions,
                hooks=self._hooks,
                pipeline=self._pipeline,
                compaction=self._compaction,
                locations=self._resolved_locations(),
            )
        return self._runtime

    def _resolved_locations(self) -> Optional[list[dict]]:
        """Where this session's tools may run, with `None` meaning one local location at the working directory."""
        return self._locations

    async def prepare_worktree(self, strategy: str = "worktree") -> str:
        """Give this session its own git worktree and run its tools there; opt-in, because it writes to disk."""
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
        """Reload this session's conversation from its checkpoint store, and say whether anything was found."""
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
        """Write this session's conversation to its checkpoint store, through LangChain's own codec rather than by hand."""
        from langchain_core.load import dumps as dump_message

        await self._checkpoints.save(
            self._session_id,
            {"conversation": [dump_message(message) for message in self.conversation]},
        )

    def _compose(self, message: str, attachments: Sequence[str | Path]) -> object:
        """The model-facing input for a turn, including attachments, through the same composition the daemon uses."""
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
            # A library caller may have no client to raise a warning event to, so this goes to the log it does have.
            logger.warning(
                "%d attached image(s) were not inlined: %s does not advertise vision support. "
                "The model has the file paths and can open them with its tools.",
                images_not_inlined, runtime.effective_model_identifier or "the session model",
            )
        return turn_input

    async def stream(
        self, message: str, *, attachments: Sequence[str | Path] = (),
    ) -> AsyncIterator[TurnEventUnion]:
        """Drive a turn, yielding each event, and keep driving turns while a goal the agent set is still open."""
        if not self._restored:
            await self.restore()
        # Somebody is here again, so a goal that had used its allowance gets it back.
        self.runtime.restore_goal_allowance()
        try:
            async for event in self.runtime.stream(self._compose(message, attachments)):
                yield event
            # A goal outlives the turn that set it, so this call is over when the goal is rather than when the model stops talking.
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
        """Drive a turn, or a goal to its end, and answer with the agent's prose."""
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
        """Fold the conversation now, yielding the same events an automatic fold does."""
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
        # Unbind what the session bound, so a caller's credentials and tracer do not outlive it.
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
