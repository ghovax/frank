"""A session's view of the durable store: the daemon's own handlers, called where the session runs."""

from __future__ import annotations

import logging
from typing import Any, Optional

from a2a.server.tasks import TaskStore
from a2a.types import Task

logger = logging.getLogger(__name__)


class DaemonTurnStore(TaskStore):
    """A :class:`TaskStore` whose writes are performed by the daemon that hosts this session."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    async def _call(self, method: str, **params: Any) -> Any:
        """Run one ingest verb directly: the daemon that would have answered over a socket is this process."""
        from langmesh.daemon.ingest import _METHODS

        handler = _METHODS.get(method)
        if handler is None:
            logger.warning("no ingest verb named %r", method)
            return None
        try:
            return await handler({"session_id": self._session_id, **params})
        except Exception:  # noqa: BLE001 — losing durability must not lose the turn
            logger.warning("persistence call %s failed", method, exc_info=True)
            return None

    # The TaskStore interface a2a expects.

    # Part of the interface the A2A handler calls through, and unused here.
    async def save(self, task: Task, context: Any = None) -> None:
        await self._call("turn.save", task=task)

    async def get(self, turn_id: str, context: Any = None) -> Optional[Task]:
        raw = await self._call("turn.get", turn_id=turn_id)
        return Task.model_validate(raw) if raw else None

    async def delete(self, turn_id: str, context: Any = None) -> None:
        await self._call("turn.delete", turn_id=turn_id)

    # The extra surface a turn uses, beyond what a2a asks for.

    async def save_turn_state(
        self,
        session_id: str,
        turn_id: str,
        messages: list,
        session_state: Optional[dict] = None,
        inherited_snapshot_id: str = "",
    ) -> None:
        await self._call(
            "turn.save_state",
            session_id=session_id,
            turn_id=turn_id,
            messages=messages,
            session_state=session_state,
            inherited_snapshot_id=inherited_snapshot_id,
        )

    async def save_session_state(self, session_id: str, session_state: dict) -> None:
        """Write the durable goal/task state alone, for a change that happened between turns."""
        await self._call(
            "turn.save_session_state", session_id=session_id, session_state=session_state
        )

    async def load_checkpoint(self, session_id: str) -> dict:
        return await self._call("turn.load_checkpoint", session_id=session_id) or {
            "messages": [],
            "inherited_snapshot_id": "",
            "inherited_message_count": 0,
        }

    async def load_session_state(self, session_id: str) -> dict:
        return await self._call("turn.load_session_state", session_id=session_id) or {}

    async def turns_for_session(self, session_id: str) -> list[Task]:
        raw = await self._call("turn.list_for_session", session_id=session_id) or []
        return [Task.model_validate(entry) for entry in raw]

    async def claim_work_habits(self, session_id: str) -> bool:
        """Claim the once-per-session work-habits acknowledgement through the daemon, since a worker is per activation."""
        result = await self._call("session.claim_work_habits", session_id=session_id)
        return bool((result or {}).get("claimed"))

    async def append_memory(
        self, session_id: str, observations: list, directives: list
    ) -> None:
        """Commit one observer's two ledgers atomically through the daemon."""
        await self._call(
            "session.append_memory",
            session_id=session_id,
            observations=observations,
            directives=directives,
        )

    async def memory_entries(self, session_id: str) -> dict[str, list]:
        """Read both live memory ledgers from one database snapshot."""
        return (
            await self._call("session.memory", session_id=session_id)
            or {"observations": [], "directives": []}
        )

    async def publish_event(self, event: dict) -> None:
        """Hand a live turn event to the daemon, so whoever is attached sees it now."""
        await self._call("session.event", event=event)

    async def publish_usage(self, usage: dict) -> None:
        """Hand the daemon the rate-limit snapshot, captured here and read by the process serving settings."""
        await self._call("session.usage", usage=usage)

    async def publish_title(self, title: str) -> None:
        """Hand the daemon a title this session generated for itself."""
        await self._call("session.title", title=title)

    async def aclose(self) -> None:
        """Nothing is held open: the store is the daemon this session runs inside."""
        return None
