"""The worker's view of the store: a client that forwards writes to the daemon.

The daemon is the sole writer. That is not an arbitrary split — the append-only store was
built around one process holding one lock across an await, and it keeps that property only
while exactly one process writes. So a worker never opens the database; it speaks the same
store interface and sends every write down a socket to the daemon, which performs it.

Reads go the same way, with one consequence worth stating: a worker must never read back
something it has just written and assume it is there. It already holds the newer copy in
memory, so nothing here needs to.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx
from a2a.server.tasks import TaskStore
from a2a.types import Task

logger = logging.getLogger(__name__)


class DaemonTurnStore(TaskStore):
    """A :class:`TaskStore` whose writes are performed by the daemon."""

    def __init__(self, socket_path: str, session_id: str, token: str) -> None:
        self._socket_path = socket_path
        self._session_id = session_id
        self._token = token
        self._client: Optional[httpx.AsyncClient] = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=self._socket_path),
                base_url="http://daemon",
                timeout=60.0,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        return self._client

    async def _call(self, method: str, **params: Any) -> Any:
        payload = {"method": method, "params": {"session_id": self._session_id, **params}}
        try:
            response = await self._http().post("/ingest", json=payload)
        except (httpx.HTTPError, OSError) as error:
            # Losing the daemon means losing durability, not the turn: the session keeps its
            # own conversation in memory and the next successful write catches up. Failing the
            # turn here would discard work the model has already done.
            logger.warning("Persistence call %s failed: %s", method, error)
            return None
        if response.status_code >= 400:
            logger.warning("Persistence call %s rejected: %s", method, response.text[:200])
            return None
        return response.json().get("result")

    # The TaskStore interface a2a expects.

    # The context argument is part of the interface the A2A handler calls through; it carries
    # per-call server state this store has no use for, but the signature must accept it.
    async def save(self, task: Task, context: Any = None) -> None:
        await self._call("turn.save", task=task.model_dump(by_alias=True, exclude_none=True, mode="json"))

    async def get(self, turn_id: str, context: Any = None) -> Optional[Task]:
        raw = await self._call("turn.get", turn_id=turn_id)
        return Task.model_validate(raw) if raw else None

    async def delete(self, turn_id: str, context: Any = None) -> None:
        await self._call("turn.delete", turn_id=turn_id)

    # The extra surface a turn uses, beyond what a2a asks for.

    async def save_turn_state(
        self, session_id: str, turn_id: str, messages: list, session_state: Optional[dict] = None
    ) -> None:
        await self._call(
            "turn.save_state",
            session_id=session_id, turn_id=turn_id, messages=messages, session_state=session_state,
        )

    async def load_checkpoint(self, session_id: str) -> list:
        return await self._call("turn.load_checkpoint", session_id=session_id) or []

    async def load_session_state(self, session_id: str) -> dict:
        return await self._call("turn.load_session_state", session_id=session_id) or {}

    async def turns_for_session(self, session_id: str) -> list[Task]:
        raw = await self._call("turn.list_for_session", session_id=session_id) or []
        return [Task.model_validate(entry) for entry in raw]

    async def claim_work_habits(self, session_id: str) -> bool:
        """Claim the once-per-session work-habits acknowledgement, through the daemon.

        Durable because a worker is per activation: a session that slept between turns must not
        re-acknowledge every time it wakes."""
        result = await self._call("session.claim_work_habits", session_id=session_id)
        return bool((result or {}).get("claimed"))

    async def publish_event(self, event: dict) -> None:
        """Hand a live turn event to the daemon so whoever is attached sees it now, rather
        than after the turn's next persistence point."""
        await self._call("session.event", event=event)

    async def publish_title(self, title: str) -> None:
        """Hand the daemon a title this session generated for itself."""
        await self._call("session.title", title=title)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
