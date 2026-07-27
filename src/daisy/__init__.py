"""Daisy as a library: the harness, without the daemon.

The harness proper — the turn loop, the tools, the prompts, the permission engine, the model
clients — has always been free of the control plane. `runtime` imports nothing from `daemon`
or `worker`; it takes what it needs by injection. What it never had was a front door, so the
only way to run a turn was to start a daemon and drive a session over a socket.

This is that front door.

    import asyncio
    from daisy import Session

    async def main() -> None:
        async with Session(agent="general-assistant", directory=".") as session:
            print(await session.ask("what does this project do?"))

    asyncio.run(main())

**Everything durable is a seam.** A library that writes where it likes is a library you cannot
embed, so each thing this one writes down — the conversation checkpoint, the background-job
record, the audit trail — is a constructor argument with an interface behind it, and the
default for each is *nothing on your disk*. Bring your own model, store, approver or observer
by passing an object with the right methods; there is no base class to inherit and no registry
to join. See :mod:`daisy.base.ports`.

    from daisy import Approval, Session

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
you — a tool that exhausts memory takes your process with it. Use `daisyd` when you want any of
those. Confinement is unaffected either way, because it was never a property of the session
process: a tool's child is confined at the moment it is spawned, identically here.

**Peers.** `create_session` and its siblings are absent unless you supply `peers`, implementing
:class:`daisy.runtime.tools.sessions.SessionAccess`. Composition means addressing another
session, and in a library there is no control plane to address one through. Ask for peers and
you are asking for the daemon.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Optional

from daisy.base.ports import (
    Approval,
    Approvals,
    Catalogue,
    Checkpoints,
    JobStore,
    MemoryCheckpoints,
    MemoryJobStore,
    Observation,
    Observer,
    SuspensionGate,
    describe_unmet,
)

__all__ = [
    "Approval",
    "Approvals",
    "Catalogue",
    "Checkpoints",
    "JobStore",
    "MemoryCheckpoints",
    "MemoryJobStore",
    "Observation",
    "Observer",
    "Session",
    "SuspensionGate",
    "__version__",
]

try:  # pragma: no cover - a source checkout has no distribution metadata
    from importlib.metadata import version as _package_version

    __version__ = _package_version("daisy")
except Exception:  # noqa: BLE001 — a missing distribution must not break an import
    __version__ = "0"


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
        agent: str | Any,
        *,
        directory: str | Path = ".",
        session_id: str = "",
        permission_mode: str = "",
        sandbox: Any = None,
        configuration: Any = None,
        # The seams. Each defaults to the least surprising thing for a program that is not a
        # daemon, which for anything durable means "in memory", not "somewhere under $HOME".
        model: Any = None,
        catalogue: Optional[Catalogue] = None,
        checkpoints: Optional[Checkpoints] = None,
        jobs: Optional[JobStore] = None,
        observer: Optional[Observer] = None,
        approvals: Optional[Approvals] = None,
        peers: Any = None,
        mcp_manager: Any = None,
    ) -> None:
        from daisy.base.configuration import GlobalConfiguration
        from daisy.base.identifiers import new_id

        self._agent = agent
        self._directory = str(Path(directory).resolve())
        self._session_id = session_id or new_id("session")
        self._permission_mode = permission_mode
        self._sandbox = sandbox
        self._peers = peers
        self._mcp_manager = mcp_manager
        # `seed=False`: reading configuration must not leave a file in the caller's home
        # directory. The CLI and the daemon seed it deliberately, because a person installing
        # Daisy needs something to edit; a program that imported us did not ask for that.
        self._configuration = configuration or GlobalConfiguration.load(seed=False)
        self._model = model
        self._catalogue = _require(Catalogue, catalogue, "catalogue")
        self._checkpoints = _require(Checkpoints, checkpoints, "checkpoints") or MemoryCheckpoints()
        self._jobs = _require(JobStore, jobs, "jobs") or MemoryJobStore()
        self._observer = _require(Observer, observer, "observer")
        self._approvals = _require(Approvals, approvals, "approvals")
        self._runtime: Any = None
        self._restored = False

    @property
    def id(self) -> str:
        """This session's identity, which is what a checkpoint is keyed by."""
        return self._session_id

    @property
    def runtime(self) -> Any:
        """The underlying :class:`~daisy.runtime.runtime.AgentRuntime`, built on first use.

        Exposed because a library that hides its own core forces every non-obvious use into a
        fork. Everything below is a convenience over it."""
        if self._runtime is None:
            from daisy.base.confinement import Profile
            from daisy.base.tuning import set_tuning, tuning_from_policy
            from daisy.runtime.runtime import AgentRuntime

            # The tuning policy is bound per task, so binding it here scopes it to the caller
            # rather than to the interpreter.
            set_tuning(tuning_from_policy(self._configuration.tuning))
            # The working directory's own `.agents` plus the packaged base layer, and
            # deliberately nothing of `$HOME`. A program that imported Daisy did not ask to
            # inherit the machine's agents, its memories, or — as the instruction loader did —
            # another product's configuration file. `daisyd` and the CLI pass a catalogue that
            # does read those, because there the person and the home directory are the same
            # person.
            catalogue = self._catalogue
            if catalogue is None:
                from daisy.base.catalogue import project_catalogue

                catalogue = project_catalogue(self._configuration, self._directory)
            agent_configuration = (
                self._agent if not isinstance(self._agent, str) else catalogue.agent(self._agent)
            )
            if agent_configuration is None:
                available = ", ".join(catalogue.agents()) or "none"
                raise LookupError(
                    f"No agent profile named {self._agent!r}. This catalogue offers: {available}."
                )
            self._runtime = AgentRuntime(
                agent_configuration=agent_configuration,
                global_configuration=self._configuration,
                session_id=self._session_id,
                working_directory=self._directory,
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
            )
        return self._runtime

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
        """Drive one turn, yielding each :class:`~daisy.runtime.turn_events.TurnEvent`.

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
        from daisy.runtime.turn_events import Done, Suspended

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
        from daisy.runtime.background import cancel_all_background_jobs

        with contextlib.suppress(Exception):
            cancel_all_background_jobs()
        if "daisy.computer.web" in sys.modules:
            with contextlib.suppress(Exception):
                sys.modules["daisy.computer.web"].close()

    async def __aenter__(self) -> "Session":
        return self

    async def __aexit__(self, *_exception: object) -> None:
        await self.aclose()
