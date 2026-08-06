"""Process-wide pub/sub for "something you are looking at changed"."""

from __future__ import annotations

import asyncio


class Broadcaster:
    """Daemon-wide pub/sub: every subscriber gets every event, so one client's change reaches the others."""

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
        """End every subscriber's stream, since an open response keeps the daemon from finishing its shutdown."""
        for queue in list(self._subscribers):
            queue.put_nowait(None)
