"""Starting sessions, watching them, and reaping them together, as one concern seen from three ends."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from langmesh.daemon.host import SessionHost
from langmesh.daemon.registry import EXITED, FAILED, SessionRecord, SessionRegistry
from langmesh.protocol.metadata import Metadata

logger = logging.getLogger(__name__)


def _daemon_token() -> str:
    from langmesh.daemon import state

    return state.daemon_token


def _resolve_locations(session_id: str):
    """The workspace's locations in the shape the runtime builds executors from, best effort."""
    from langmesh.commons.services.locations import _resolve_session_locations

    try:
        return _resolve_session_locations(session_id)
    except Exception:  # noqa: BLE001 — a session without locations still runs
        logger.warning("could not resolve locations for %s", session_id, exc_info=True)
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _close_subscribers(session_id: str) -> None:
    """Tell every subscriber of this session's stream that it has ended, so an attach does not hang forever."""
    from langmesh.daemon import state

    with contextlib.suppress(Exception):
        state.event_bus.complete(session_id)


class SessionLifecycle:
    """Owns the transition of a record into a hosted session, and back out again."""

    def __init__(
        self,
        registry: SessionRegistry,
        host: SessionHost,
        *,
        on_change: Optional[Callable[[], None]] = None,
    ) -> None:
        self._registry = registry
        self._host = host
        self._on_change = on_change

    def _changed(self) -> None:
        if self._on_change is not None:
            with contextlib.suppress(Exception):
                self._on_change()

    async def start(self, record: SessionRecord) -> bool:
        """Give a record a live executor, which costs about what building the object costs."""
        started = await self._host.start(
            record, locations=_resolve_locations(record.id), daemon_token=_daemon_token()
        )
        if not started:
            self._registry.end(
                record.id, outcome=FAILED, reason="the session could not be built", updated_at=_now()
            )
            self._changed()
            return False
        self._registry.mark(record.id, hosted=True, updated_at=_now())
        self._changed()
        return True

    async def _tell_parent(self, record) -> None:
        """Tell a session that one of its children is over, since a peer's own report is the whole return path."""
        from langmesh.daemon.state import relay_to_session

        parent = self._registry.get(record.parent) if record.parent else None
        if parent is None or not parent.is_live:
            return
        outcome = record.exit_reason or ("finished" if record.outcome == EXITED else record.outcome)
        text = (
            f"Session {record.id} ({record.agent}), which you created, has ended without "
            f"reporting back: {outcome}."
        )
        try:
            await relay_to_session(parent, "message/send", {
                "id": parent.id,
                "parts": [{"kind": "text", "text": text}],
                "metadata": {Metadata.PEER_SENDER: record.id},
            })
        except Exception:  # noqa: BLE001 — a notice that cannot be delivered is not a failure
            logger.debug("could not tell %s that %s ended", parent.id, record.id, exc_info=True)

    async def reap(self, session_id: str, *, reason: str = "", skip_self: bool = False) -> int:
        """Take a session and everything under it down, children first."""
        record = self._registry.get(session_id)
        if record is None:
            return 0
        descendants = [record for record in self._registry.descendants_of(session_id) if record.is_live]
        # A goal describes work in progress, so it ends with the session that was pursuing it.
        from langmesh.base import toolbox
        from langmesh.daemon import state as daemon_state

        for ending in ([] if skip_self else [record]) + descendants:
            daemon_state._session_goals.pop(ending.id, None)
            toolbox.discard(ending.id)
        for descendant in descendants:
            self._registry.end(
                descendant.id, outcome=EXITED, updated_at=_now(),
                reason=reason or "parent session was reaped",
            )
        # Stopped together rather than one at a time, since each carries its own grace period.
        await asyncio.gather(*(self._stop(record.id) for record in descendants), return_exceptions=True)
        reaped = len(descendants)
        if not skip_self and record.is_live:
            self._registry.end(session_id, outcome=EXITED, reason=reason, updated_at=_now())
            await self._stop(session_id)
            reaped += 1
        self._changed()
        # After the stops, so the notice describes a session that is actually over, and only for the one asked about.
        if not skip_self:
            await self._tell_parent(self._registry.get(session_id) or record)
        return reaped

    async def sleep(self, session_id: str) -> bool:
        """Take a live session's executor away, leaving the session itself intact."""
        record = self._registry.get(session_id)
        if record is None or not record.is_live or not self._host.hosts(session_id):
            return False
        logger.info("sleeping session %s", session_id)
        await self._host.stop(session_id)
        self._registry.sleep(session_id, updated_at=_now())
        # Deliberately not closing subscribers, since a watcher should see the next turn when the session wakes.
        self._changed()
        return True

    async def sleep_all(self) -> int:
        """Drop every executor while keeping every session, which is what a durable registry makes shutdown mean."""
        hosted = self._host.hosted()
        await asyncio.gather(*(self.sleep(session_id) for session_id in hosted), return_exceptions=True)
        return len(hosted)

    async def _stop(self, session_id: str) -> None:
        await self._host.stop(session_id)
        self._registry.mark(session_id, hosted=False)
        _close_subscribers(session_id)

    async def aclose(self) -> None:
        """Sleep every hosted session; the records outlive the daemon and are what a restart reads."""
        await self.sleep_all()
