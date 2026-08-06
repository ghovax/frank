"""The chain a tool call passes through, where `proceed` is the rest of it and ordering is explicit."""

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
        """Pass `call` down the chain, built inside out so the first middleware is the outermost frame."""
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
