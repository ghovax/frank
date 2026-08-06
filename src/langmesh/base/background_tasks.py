"""Fire-and-forget tasks that survive until they finish, since `create_task` keeps only a weak reference."""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, Optional

# Strong references to in-flight fire-and-forget tasks; a task removes itself when it settles.
_pending: set[asyncio.Task] = set()


def spawn_background_task(coro: Coroutine[Any, Any, Any], *, name: Optional[str] = None) -> asyncio.Task:
    """Schedule ``coro`` and hold a strong reference, so it cannot be collected before it completes."""
    task = asyncio.create_task(coro, name=name)
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task
