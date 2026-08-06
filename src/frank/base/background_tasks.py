"""Fire-and-forget asyncio tasks that survive until they finish."""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, Optional

# Strong references to in-flight fire-and-forget tasks; a task removes itself when it settles.
_pending: set[asyncio.Task] = set()


def spawn_background_task(coro: Coroutine[Any, Any, Any], *, name: Optional[str] = None) -> asyncio.Task:
    """Schedule ``coro`` on the running loop and keep it alive until it completes."""
    task = asyncio.create_task(coro, name=name)
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task
