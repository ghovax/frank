"""The daemon's process-wide singletons, and the one place a session's verb is called."""

from __future__ import annotations

import asyncio
import logging
from typing import Any


logger = logging.getLogger(__name__)


class SessionEventBus:
    """Per-session fan-out of the events a turn emits, so several watchers can follow one session without polling."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    def subscribe(self, session_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(session_id, []).append(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        queues = self._subscribers.get(session_id)
        if not queues:
            return
        if queue in queues:
            queues.remove(queue)
        if not queues:
            self._subscribers.pop(session_id, None)

    def publish(self, session_id: str, event: dict) -> None:
        for queue in list(self._subscribers.get(session_id, [])):
            queue.put_nowait(event)

    def complete(self, session_id: str) -> None:
        """Close every watcher's stream — the session has ended."""
        for queue in list(self._subscribers.get(session_id, [])):
            queue.put_nowait(None)

    def complete_all(self) -> None:
        """Close every watcher of every session, since the daemon cannot finish shutting down while a stream is open."""
        for session_id in list(self._subscribers):
            self.complete(session_id)


# The supervision singletons: what a session's existence depends on, as distinct from what the browser surface needs.

registry: Any = None  # SessionRegistry
host: Any = None  # SessionHost
lifecycle: Any = None  # SessionLifecycle

event_bus = SessionEventBus()

# Re-exported from the workspace layer, because they are read here constantly.


def __getattr__(name: str) -> Any:
    """Read a workspace singleton through this module, resolved lazily because they are set after this one is imported."""
    from langmesh.commons import state as commons_state

    if hasattr(commons_state, name):
        return getattr(commons_state, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# The running and awaiting sets live on the workspace module and reach this one through the lookup above.
_title_tasks: set = set()
# Long-lived tasks the daemon owns, held so teardown can cancel them.
_watchers: list = []
_mcp_start_task = None
_remote_start_task = None
# The HTTP client the push sender borrows, closed with everything else on shutdown.
_push_client = None

# The daemon's own addresses and the token that guards them, written where a client can find them.
daemon_socket: str = ""
daemon_token: str = ""


async def reset_live_session_runtimes() -> None:
    """Tell the sessions being hosted to rebuild their runtime; a record with no executor has none to drop."""
    if host is None:
        return
    await asyncio.gather(
        *(dispatch_to_session(session_id, "session/reset", {}) for session_id in host.hosted()),
        return_exceptions=True,
    )


async def refresh_workspace_locations(workspace_id: str) -> None:
    """Tell every live session in a workspace that its locations have changed."""
    if registry is None or not workspace_id:
        return
    from langmesh.commons.services.locations import _resolve_session_locations

    live = [
        record
        for record in registry.live()
        if record.workspace_id == workspace_id and host is not None and host.hosts(record.id)
    ]
    if not live:
        return

    async def push(record) -> None:
        locations = await asyncio.to_thread(_resolve_session_locations, record.id)
        await relay_to_session(record, "session/locations", {"locations": locations})

    await asyncio.gather(*(push(record) for record in live), return_exceptions=True)


async def wake_then_relay(record, method: str, params: dict) -> dict:
    """Forward a command to a session, building its executor first if this daemon is not hosting it yet."""
    if host is not None and not host.hosts(record.id):
        await _wake(record)
    return await dispatch_to_session(record.id, method, params)


_wake_locks: dict[str, asyncio.Lock] = {}


async def _wake(record) -> None:
    """Give a session an executor again, from the record that already holds everything the build needs."""
    lock = _wake_locks.setdefault(record.id, asyncio.Lock())
    async with lock:
        # Re-checked inside the lock, since the waiter may be the second of two and the first has already done this.
        if host is not None and host.hosts(record.id):
            return
        if lifecycle is None:
            raise RuntimeError(
                f"Session {record.id} has no executor and there is nothing to build one."
            )
        if not await lifecycle.start(record):
            raise RuntimeError(f"Session {record.id} could not be started; see the daemon log.")


async def dispatch_to_session(session_id: str, method: str, params: dict) -> dict:
    """Call one of a hosted session's verbs, which is what used to cross a socket."""
    if host is None:
        raise RuntimeError("This daemon is hosting nothing.")
    return await host.dispatch(session_id, method, params)


async def relay_to_session(record, method: str, params: dict) -> dict:
    """Address a session by its record, which is how every caller outside this module reaches one."""
    return await dispatch_to_session(record.id, method, params)
