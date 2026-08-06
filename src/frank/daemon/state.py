"""The daemon's process-wide singletons, and the one place a command is relayed to a session."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from frank.base.serialization import upstream_detail

logger = logging.getLogger(__name__)


class SessionEventBus:
    """Per-session fan-out of the events a turn emits, so several watchers can follow one session without any of them polling."""

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
        """Close every watcher of every session, for the same reason `Broadcaster.close` exists: the daemon cannot finish shutting down while a stream is still open, and the stream will not end until something tells it to."""
        for session_id in list(self._subscribers):
            self.complete(session_id)


# The supervision singletons: what a session's *existence* depends on.

registry: Any = None            # SessionRegistry
prototype: Any = None           # PrototypeClient
lifecycle: Any = None           # SessionLifecycle

event_bus = SessionEventBus()

# Re-exported from the workspace layer, because these are read here constantly and a daemon that had to spell out which module each singleton came from would be a daemon whose every line advertised a split nobody reading it needs to think about.


def __getattr__(name: str) -> Any:
    """Read a workspace singleton through this module."""
    from frank.hub import state as hub_state

    if hasattr(hub_state, name):
        return getattr(hub_state, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# `_running_contexts` and `_awaiting_input_contexts` live on the workspace module and reach this one through the `__getattr__` above.
_title_tasks: set = set()
# Long-lived tasks the daemon owns: the on-disk watchers, and the two background connects (MCP servers, remote peers) that must never hold up boot.
_watchers: list = []
_mcp_start_task = None
_remote_start_task = None
# The HTTP client the push sender borrows, closed with everything else on shutdown.
_push_client = None

# The daemon's own addresses and the token that guards them, written to the runtime directory at startup so a client can find them without being told.
daemon_socket: str = ""
daemon_token: str = ""


async def reset_live_session_runtimes() -> None:
    """Tell every running session to rebuild its runtime on the next turn."""
    if registry is None:
        return
    live = list(registry.live())
    if not live:
        return
    await asyncio.gather(
        *(relay_to_session(record, "session/reset", {}) for record in live),
        return_exceptions=True,
    )


async def refresh_workspace_locations(workspace_id: str) -> None:
    """Tell every live session in a workspace that its environments have changed."""
    if registry is None or not workspace_id:
        return
    from frank.hub.services.locations import _resolve_session_locations

    live = [
        record for record in registry.live()
        if record.workspace_id == workspace_id and not record.asleep
    ]
    if not live:
        return

    async def push(record) -> None:
        locations = await asyncio.to_thread(_resolve_session_locations, record.id)
        await relay_to_session(record, "session/locations", {"locations": locations})

    await asyncio.gather(*(push(record) for record in live), return_exceptions=True)


async def wake_then_relay(record, method: str, params: dict) -> dict:
    """Forward a command to a session, forking it a worker first if it has none."""
    if record.asleep:
        await _wake(record)
    try:
        return await relay_to_session(record, method, params)
    except SessionUnreachable:
        # Not an edge case: this is the ordinary path for the second and every later message in a conversation.
        logger.info("session %s had no worker for %s; waking it and retrying", record.id, method)
        slept = registry.sleep(record.id)
        await _wake(slept if slept is not None else record)
        return await relay_to_session(slept if slept is not None else record, method, params)


class SessionUnreachable(RuntimeError):
    """No worker answered on the session's socket."""


_wake_locks: dict[str, asyncio.Lock] = {}


async def _wake(record) -> None:
    """Give a sleeping session a worker again."""
    lock = _wake_locks.setdefault(record.id, asyncio.Lock())
    async with lock:
        # Re-checked inside the lock: the client that waited on it may have been the second of two, and the first has already done this.
        if not record.asleep:
            return
        if lifecycle is None:
            raise RuntimeError(f"Session {record.id} is asleep and there is nothing to wake it.")
        if not await lifecycle.start(record):
            raise RuntimeError(
                f"Session {record.id} is asleep and could not be woken; see the daemon log."
            )


async def relay_to_session(record, method: str, params: dict) -> dict:
    """Forward a client's command to the session that owns it."""
    transport = httpx.AsyncHTTPTransport(uds=str(record.socket_path))
    payload = {"method": method, "params": {key: value for key, value in params.items() if key != "id"}}
    try:
        async with httpx.AsyncClient(transport=transport, timeout=120.0) as client:
            response = await client.post(
                "http://session/rpc",
                json=payload,
                headers={"Authorization": f"Bearer {record.token}"},
            )
    except (httpx.ConnectError, httpx.ConnectTimeout, OSError) as error:
        # Nothing accepted the connection, so there is no worker right now — either it died without being reaped, or it simply exited at the end of its turn.
        raise SessionUnreachable(f"Session {record.id} is not reachable ({error}).") from error
    except httpx.HTTPError as error:
        # Something answered and then took too long, or the read failed partway.
        raise RuntimeError(
            f"Session {record.id} did not answer {method} in time ({error}). Its worker is "
            f"running but did not respond; the turn may still be in progress."
        ) from error
    if response.status_code >= 400:
        raise RuntimeError(f"Session {record.id} rejected {method}: {upstream_detail(response.text)}")
    body = response.json()
    return body.get("result", body)
