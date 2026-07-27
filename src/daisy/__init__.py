"""Daisy as a library: the harness, without the daemon.

The harness proper — the turn loop, the tools, the prompts, the permission engine, the model
clients — has always been free of the control plane. `runtime` imports nothing from `daemon`
or `worker`; it takes what it needs by injection. What it never had was a front door, so the
only way to run a turn was to start a daemon and drive a session over a socket.

This is that front door. It is for embedding the harness in another program, for a terminal
interface that wants to share code with the browser one rather than reimplement it, and for a
one-shot run in a script or a test.

    import asyncio
    from daisy import Session

    async def main() -> None:
        async with Session(agent="general-assistant", directory=".") as session:
            print(await session.ask("what does this project do?"))

    asyncio.run(main())

**What a library session is not.** It is an object in your process, not a process of its own,
so it has none of the three properties the daemon exists to provide: it is not addressable
from outside, it does not outlive the program that made it, and it is not crash-isolated from
you — a tool that exhausts memory takes your process with it. Use `daisyd` when you want any of
those. Confinement is unaffected either way, because it was never a property of the session
process: a tool's child is confined at the moment it is spawned, identically here.

**Peers.** `create_session` and its siblings are absent unless you supply a `session_access`
implementing :class:`daisy.runtime.tools.sessions.SessionAccess`. Composition means addressing
another session, and in a library there is no control plane to address one through. Ask for
peers and you are asking for the daemon.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Optional

__all__ = ["Session", "__version__"]

try:  # pragma: no cover - a source checkout has no distribution metadata
    from importlib.metadata import PackageNotFoundError, version as _package_version

    __version__ = _package_version("daisy")
except Exception:  # noqa: BLE001 — a missing distribution must not break an import
    __version__ = "0"


class Session:
    """One agent, driven turn by turn, in this process.

    Built lazily: constructing a session reads configuration and nothing else, so a session
    that is never asked anything costs no model client and no MCP connection. The first turn
    pays for both.
    """

    def __init__(
        self,
        agent: str,
        *,
        directory: str | Path = ".",
        permission_mode: str = "",
        sandbox: Any = None,
        session_access: Any = None,
        mcp_manager: Any = None,
        configuration: Any = None,
    ) -> None:
        from daisy.base.configuration import GlobalConfiguration

        self._agent = agent
        self._directory = str(Path(directory).resolve())
        self._permission_mode = permission_mode
        self._sandbox = sandbox
        self._session_access = session_access
        self._mcp_manager = mcp_manager
        self._configuration = configuration or GlobalConfiguration.load()
        self._runtime: Any = None

    @property
    def runtime(self) -> Any:
        """The underlying :class:`~daisy.runtime.runtime.AgentRuntime`, built on first use.

        Exposed because a library that hides its own core forces every non-obvious use into a
        fork. Everything below is a convenience over it."""
        if self._runtime is None:
            from daisy.base.configuration import load_agent_configuration
            from daisy.base.confinement import Profile
            from daisy.base.tuning import set_tuning, tuning_from_policy
            from daisy.runtime.runtime import AgentRuntime

            # The tuning policy is bound per task, so binding it here scopes it to the caller
            # rather than to the interpreter.
            set_tuning(tuning_from_policy(self._configuration.tuning))
            agent_configuration = load_agent_configuration(
                self._agent, self._configuration.agent_directories_for(self._directory)
            )
            self._runtime = AgentRuntime(
                agent_configuration=agent_configuration,
                global_configuration=self._configuration,
                working_directory=self._directory,
                project_directory=self._directory,
                permission_mode=self._permission_mode,
                sandbox=self._sandbox if self._sandbox is not None else Profile(),
                session_access=self._session_access,
                mcp_manager=self._mcp_manager,
            )
        return self._runtime

    async def stream(self, message: str) -> AsyncIterator[Any]:
        """Drive one turn, yielding each :class:`~daisy.runtime.turn_events.TurnEvent`.

        The events are the harness's own vocabulary — text chunks, tool calls, tool results,
        usage, suspensions — the same ones a session streams to a client over its socket."""
        async for event in self.runtime.stream(message):
            yield event

    async def ask(self, message: str) -> str:
        """Drive one turn and answer with the agent's prose.

        A suspension has nowhere to go in a library — there is no client to raise a permission
        prompt to — so a turn that needs a human decision raises rather than hanging on a
        gate nobody is watching. Choose a permission mode the work fits, or drive
        :meth:`stream` and answer the gates yourself."""
        from daisy.runtime.turn_events import Done, Suspended

        answer = ""
        async for event in self.stream(message):
            if isinstance(event, Suspended):
                raise PermissionError(
                    "This turn needs a human decision, and a library session has no client to "
                    "ask. Drive `stream()` and answer the gate, or create the session in a "
                    "permission mode that does not gate this work."
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
