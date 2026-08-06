"""The chain a tool call passes through on its way to running.

Middleware wraps one call. `proceed` is the rest of the chain, so ordering is explicit at the
call site — `[Timed(), RetryTransient()]` times the retries, and reversing it retries the
timing — and each layer can be tested with a stub `proceed` and nothing else.

It wraps the harness's own tools as well as a caller's, which is the asymmetry this exists to
remove: before it, a cross-cutting concern could only live inside a tool, so a caller could
have timing on their tools and never on `bash`.

Unlike a hook, middleware is *not* absorbed on failure. A hook watches; middleware is in the
call path and decides whether the call happens. Swallowing an exception there would turn a
retry layer's bug into a silently skipped tool, so a raising middleware fails its call the way
a raising tool does.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Sequence


class ToolPipeline:
    """Runs a call through its middleware, outermost first."""

    def __init__(self, middleware: Sequence[Any] = ()) -> None:
        self._middleware = list(middleware)

    @property
    def empty(self) -> bool:
        return not self._middleware

    async def run(self, call: Any, execute: Callable[[Any], Awaitable[Any]]) -> Any:
        """Pass `call` down the chain, ending in `execute`.

        Built from the inside out so the first middleware is the outermost frame, which is the
        order a reader expects from the list they wrote.
        """
        proceed = execute
        for middleware in reversed(self._middleware):
            proceed = _Layer(middleware, proceed)
        return await proceed(call)


class _Layer:
    """One middleware bound to the rest of the chain."""

    def __init__(self, middleware: Any, proceed: Callable[[Any], Awaitable[Any]]) -> None:
        self._middleware = middleware
        self._proceed = proceed

    async def __call__(self, call: Any) -> Any:
        return await self._middleware.run(call, self._proceed)


__all__ = ["ToolPipeline"]
