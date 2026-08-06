"""The live sessions this daemon is running, as objects rather than processes."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any, Optional

from langmesh.base.tuning import Tuning, set_tuning, tuning_from_policy
from langmesh.daemon.registry import SessionRecord

logger = logging.getLogger(__name__)


class SessionUnreachable(RuntimeError):
    """No executor is hosting this session, which building one resolves."""


@dataclass
class _Hosted:
    """One live session: its executor, and the tuning its own configuration asks for."""

    executor: Any
    tuning: Tuning


class SessionHost:
    """Holds one executor per live session, and is the only thing that calls into one."""

    def __init__(self) -> None:
        self._sessions: dict[str, _Hosted] = {}
        # Both directions of the same fact: attribution reads one, ending a session reads the other.
        self._session_of_group: dict[int, str] = {}
        self._groups_of_session: dict[str, set[int]] = {}
        self._starting: dict[str, asyncio.Lock] = {}

    def hosts(self, session_id: str) -> bool:
        return session_id in self._sessions

    def hosted(self) -> list[str]:
        return list(self._sessions)

    def executor(self, session_id: str) -> Optional[Any]:
        held = self._sessions.get(session_id)
        return held.executor if held is not None else None

    async def start(self, record: SessionRecord, locations: Any = None, daemon_token: str = "") -> bool:
        """Build this session's executor and hold it, which is the whole of starting a session."""
        lock = self._starting.setdefault(record.id, asyncio.Lock())
        async with lock:
            if record.id in self._sessions:
                return True
            from langmesh.commons import state as commons_state
            from langmesh.worker.session import SessionExecutor

            configuration = commons_state.global_configuration
            assert configuration is not None
            try:
                executor = SessionExecutor(
                    session_id=record.id,
                    agent_name=record.agent,
                    working_directory=record.working_directory,
                    runtime_working_directory=record.runtime_working_directory or record.working_directory,
                    permission_mode=record.permission_mode,
                    sandbox=record.sandbox or {},
                    workspace_id=record.workspace_id,
                    locations=locations,
                    parent=record.parent,
                    token=record.token,
                    daemon_token=daemon_token,
                    global_configuration=configuration,
                )
                await executor.start()
            except Exception:  # noqa: BLE001 — a session that cannot be built is a failed start, not a crash
                logger.exception("could not build session %s", record.id)
                return False
            # Resolved once here rather than per call: it is a property of the session, not of the request.
            self._sessions[record.id] = _Hosted(executor, tuning_from_policy(configuration.tuning))
            self._groups_of_session.setdefault(record.id, set())
            return True

    async def dispatch(self, session_id: str, method: str, params: dict) -> dict:
        """Call one of a session's verbs, which is what used to be a request to its socket."""
        from langmesh.worker.server import METHODS

        held = self._sessions.get(session_id)
        if held is None:
            raise SessionUnreachable(f"Session {session_id} is not being hosted.")
        handler = METHODS.get(method)
        if handler is None:
            raise RuntimeError(f"Session {session_id} has no verb {method!r}.")
        # Bound per call, not at start: a task inherits the context it was created in, and this is that task.
        set_tuning(held.tuning)
        return await handler(held.executor, {key: value for key, value in params.items() if key != "id"})

    def note_child_group(self, session_id: str, group: int) -> None:
        """Remember a process group a session's tool child leads, in both directions."""
        if not session_id or not group or session_id not in self._sessions:
            return
        self._session_of_group[group] = session_id
        self._groups_of_session.setdefault(session_id, set()).add(group)

    def session_of_group(self, group: int) -> str:
        """Which session owns a process group, which is how a call from a tool child is attributed."""
        return self._session_of_group.get(group, "")

    async def stop(self, session_id: str) -> bool:
        """Drop a session's executor, which is what ends its tool children too."""
        held = self._sessions.pop(session_id, None)
        for group in self._groups_of_session.pop(session_id, set()):
            self._session_of_group.pop(group, None)
        self._starting.pop(session_id, None)
        if held is None:
            return False
        # `aclose` cancels the background jobs, and each one ends its own child from the handle it holds.
        with contextlib.suppress(Exception):
            await held.executor.aclose()
        return True

    async def aclose(self) -> None:
        """Drop every session this daemon is hosting, which shutdown does before the records are left alone."""
        for session_id in list(self._sessions):
            await self.stop(session_id)
