"""The daemon's process-wide singletons, and the one place a command is relayed to a session.

Everything the control plane touches hangs off this module: the session registry, the worker
pool, the lifecycle, the sole-writer stores, and the two fan-out buses. It is a module of
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
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class Broadcaster:
    """Daemon-wide pub/sub. Every subscriber receives every event, which is how a change made
    by one client (a new session, an edited agent) reaches all the others."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait(event)

    def close(self) -> None:
        """End every subscriber's stream. Used when the daemon is going down: an open SSE
        response keeps the server from finishing its shutdown, so the streams have to be told
        to end before the servers are asked to stop, not after."""
        for queue in list(self._subscribers):
            queue.put_nowait(None)


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


# The singletons.

registry: Any = None            # SessionRegistry
pool: Any = None                # WorkerPool
lifecycle: Any = None           # SessionLifecycle
turn_store: Any = None          # AppendOnlyTaskStore
global_configuration: Any = None
session_factory: Any = None
async_engine: Any = None
mcp_manager: Any = None
remote_agent_manager: Any = None
file_url_signer: Any = None
file_lease_manager: Any = None
workspace_manager: Any = None
push_configuration_store: Any = None
push_sender: Any = None
terminal_manager: Any = None
composio_servers: dict = {}
# The agent *profiles* a session could be created with, as A2A cards, rebuilt whenever the
# agent or skill files change. Distinct from the session registry: this is what could exist,
# that is what does.
agent_cards: dict = {}
capture_queue: Any = None
chatgpt_login_flow: Any = None
proxy_client: Any = None
main_loop: Any = None

broadcaster = Broadcaster()
event_bus = SessionEventBus()

# Per-session liveness the daemon learns from the event stream rather than from the registry:
# `_running_contexts` counts the turns a session currently has in flight (a session can be
# live but idle), and `_awaiting_input_contexts` marks the ones parked on a human decision.
# The registry knows whether a *process* is alive; these know what it is doing.
_running_contexts: dict[str, int] = {}
_awaiting_input_contexts: set[str] = set()
# Strong references to in-flight title generations, so a task is not collected mid-flight.
_title_tasks: set = set()
# Long-lived tasks the daemon owns: the on-disk watchers, and the two background connects
# (MCP servers, remote peers) that must never hold up boot. Held so teardown can cancel them.
_watchers: list = []
_mcp_start_task = None
_remote_start_task = None
# The HTTP client the push sender borrows, closed with everything else on shutdown.
_push_client = None

# The daemon's own addresses and the token that guards them, written to the runtime directory
# at startup so a client can find them without being told.
daemon_socket: str = ""
daemon_port: int = 0
daemon_token: str = ""

configuration_lock = asyncio.Lock()
last_written_configuration_digest: Optional[str] = None


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


async def relay_to_session(record, method: str, params: dict) -> dict:
    """Forward a client's command to the session that owns it.

    Only clients come through here — a session addressing a peer opens that peer's socket
    itself. The session's capability token is attached from the registry, so a client that has
    already proved itself to the daemon does not need to hold every session's token as well.
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
    except (httpx.HTTPError, OSError) as error:
        # A session whose socket has gone is a session that died without being reaped. Say that
        # plainly rather than surfacing a connection error the caller cannot act on.
        raise RuntimeError(f"Session {record.id} is not reachable ({error}).") from error
    if response.status_code >= 400:
        raise RuntimeError(f"Session {record.id} rejected {method}: {response.text[:400]}")
    body = response.json()
    return body.get("result", body)
