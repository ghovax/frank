"""The daemon's process-wide singletons, and the one place a command is relayed to a session.

Everything the control plane touches hangs off this module: the session registry, the
prototype the daemon forks sessions out of, the lifecycle, the sole-writer stores, and the two
fan-out buses. It is a module of
globals because there is exactly one daemon per user and these are its parts, not something
that could sensibly exist twice.

The relay lives here too. A session's commands belong to its own socket, but the desktop
client cannot open one, so the daemon forwards on its behalf. That is a deliberate exception
for clients, not a general routing layer: sessions talking to each other never come through
here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from frank.base.serialization import upstream_detail

logger = logging.getLogger(__name__)


class SessionEventBus:
    """Per-session fan-out of the events a turn emits, so several watchers can follow one
    session without any of them polling.

    A worker streams its turn events to the daemon as they are persisted; this is where they
    reach whoever is attached. Nothing is journaled here: an attaching client reads the
    persisted snapshot first and then joins the live tail, so there is no gap to replay.
    """

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
        """Close every watcher of every session, for the same reason `Broadcaster.close`
        exists: the daemon cannot finish shutting down while a stream is still open, and the
        stream will not end until something tells it to."""
        for session_id in list(self._subscribers):
            self.complete(session_id)


# The supervision singletons: what a session's *existence* depends on.

registry: Any = None            # SessionRegistry
prototype: Any = None           # PrototypeClient
lifecycle: Any = None           # SessionLifecycle

event_bus = SessionEventBus()

# Re-exported from the workspace layer, because these are read here constantly and a daemon that had to spell out which module each singleton came from would be a daemon whose every line advertised a split nobody reading it needs to think about.


def __getattr__(name: str) -> Any:
    """Read a workspace singleton through this module.

    Deliberately `__getattr__` rather than an import at load time: these are set *after* this
    module is imported, so a bound name would capture `None` and keep it forever. A module-level
    `__getattr__` (PEP 562) resolves on each access, which is the behaviour every reader already
    assumes `state.session_factory` has."""
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
    """Tell every running session to rebuild its runtime on the next turn.

    Configuration that a session already read — its model, its tool tuning, its MCP servers —
    is cached inside the session's process, so a change made in Settings would otherwise only
    reach sessions started afterwards. This used to be a loop over in-process executors; with
    a session being a process, it is a message to each of them.

    Best effort by design: a session that has died or is mid-teardown simply misses it, and a
    settings save must not fail because one session was unreachable."""
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
    """Tell every live session in a workspace that its environments have changed.

    The set a session may address is resolved once, at `session.create`, and travels in the
    assignment — which is right, but it left a workspace edit reaching only the sessions
    created after it. Adding a folder to the workspace you were already talking in did nothing
    the conversation could see.

    Only sessions with a worker are told. A sleeping one has nothing to inform: its next worker
    is forked with a freshly resolved set, so it already wakes up current.

    Best effort by design, exactly like the runtime reset beside it: a session mid-teardown
    misses the push and re-reads the workspace on its next fork, and saving a workspace must
    not fail because one session was unreachable."""
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
    """Forward a command to a session, forking it a worker first if it has none.

    **This is the whole of "a session is a record, not a process", and it is one function.**
    Every command that needs a live session already funnels through the relay — `session.send`,
    `respond`, `compact`, `jobs.*`, `turn.cancel` — so putting the wake in front of it is what
    makes sleeping safe everywhere at once, rather than in each caller.

    Reads deliberately do not come through here. `get`, `list`, `tree`, `history` and `attach`
    are answered from the registry and the turn store, so looking at a sleeping session does
    not wake it — which is what makes sleeping worth doing at all.

    The wake is serialised per session by a lock, because two clients messaging a sleeping
    session at the same moment must produce one worker, not two. That is not a nicety: two
    workers on one session id would both bind the same socket path and both believe they own
    the conversation.
    """
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
    """No worker answered on the session's socket.

    Distinct from a worker that answered and refused: this one is recoverable by waking.
    """


_wake_locks: dict[str, asyncio.Lock] = {}


async def _wake(record) -> None:
    """Give a sleeping session a worker again.

    The record already holds everything the assignment needs — agent, directories, permission
    mode, sandbox, parent — because all of it was fixed when the session was created and none
    of it has been re-derived since. That is what makes waking a replay rather than a
    reconstruction: the woken worker is handed exactly what the original was handed, including
    the same capability token, which is derivable precisely so that this works.
    """
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
    """Forward a client's command to the session that owns it.

    Only clients come through here — a session addressing a peer opens that peer's socket
    itself. The session's capability token is derived from its id, so a client that has already
    proved itself to the daemon does not need to hold every session's token as well.

    Prefer :func:`wake_then_relay` for anything that needs the session to *act*. This is the
    bare relay, for the one caller that already knows a worker is there.
    """
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
