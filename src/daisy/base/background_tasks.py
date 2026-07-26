"""Fire-and-forget asyncio tasks that survive until they finish.

``asyncio.create_task`` only keeps a *weak* reference to the task it returns: if the caller
does not hold the result, the event loop can garbage-collect the task mid-flight and the
coroutine silently stops running. Every place that wants to kick off background work and
walk away — a durable-input resume driver, a best-effort allow-rule persist, a remote-agent
manager start, an OAuth-completion poll — routes through :func:`spawn_background_task`, which
parks a strong reference in a module-level set and drops it in a done-callback. That is the
whole contract: start it, and it runs to completion (or its own exception) regardless of
what the caller does next.
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, Optional

# Strong references to in-flight fire-and-forget tasks; a task removes itself when it settles.
_pending: set[asyncio.Task] = set()


def spawn_background_task(coro: Coroutine[Any, Any, Any], *, name: Optional[str] = None) -> asyncio.Task:
    """Schedule ``coro`` on the running loop and keep it alive until it completes.

    A thin wrapper over ``asyncio.create_task`` that retains a strong reference so the task
    cannot be collected before it finishes. Raises ``RuntimeError`` if there is no running
    loop, exactly like ``create_task`` (callers that may run outside a loop guard for it)."""
    task = asyncio.create_task(coro, name=name)
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task
