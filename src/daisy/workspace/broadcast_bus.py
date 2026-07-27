"""Process-wide pub/sub for "something you are looking at changed".

In the workspace layer rather than the daemon's because everything that publishes to it is a
workspace change — a project created, an agent edited, a setting saved — and because the
browser surface subscribes to it. The daemon publishes too (a session appeared, a session
ended), which is composition rather than ownership: it imports the workspace, the workspace
does not import it.
"""

from __future__ import annotations

import asyncio


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
        response keeps the daemon from finishing its shutdown, so the streams have to be told
        to end before the servers are asked to stop, not after."""
        for queue in list(self._subscribers):
            queue.put_nowait(None)
