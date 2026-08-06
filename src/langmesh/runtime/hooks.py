"""Running a turn's hooks safely: a hook is someone else's code on the critical path."""

from __future__ import annotations

import logging
from typing import Any, Sequence

logger = logging.getLogger(__name__)


class MaximumToolCalls:
    """Stop a turn making more than `maximum` tool calls, counted across the turn rather than per batch."""

    def __init__(self, maximum: int) -> None:
        if maximum < 0:
            raise ValueError(f"maximum must not be negative, got {maximum}.")
        self._maximum = maximum
        self._used = 0

    async def before_tools(self, calls: list[dict]) -> list[dict]:
        remaining = max(0, self._maximum - self._used)
        self._used += min(len(calls), remaining)
        return calls[:remaining]


class HookRunner:
    """Calls a turn's hooks in order, and absorbs their failures."""

    def __init__(self, hooks: Sequence[Any] = ()) -> None:
        self._hooks = list(hooks)

    @property
    def empty(self) -> bool:
        return not self._hooks

    async def before_model(self, messages: list) -> list:
        for hook in self._hooks:
            method = getattr(hook, "before_model", None)
            if method is None:
                continue
            try:
                returned = await method(messages)
            except Exception:  # noqa: BLE001 — a hook must not fail the turn
                logger.warning("%s.before_model raised; skipping it", type(hook).__name__, exc_info=True)
                continue
            if isinstance(returned, list):
                messages = returned
        return messages

    async def before_tools(self, calls: list[dict]) -> list[dict]:
        for hook in self._hooks:
            method = getattr(hook, "before_tools", None)
            if method is None:
                continue
            try:
                returned = await method(calls)
            except Exception:  # noqa: BLE001
                logger.warning("%s.before_tools raised; skipping it", type(hook).__name__, exc_info=True)
                continue
            if isinstance(returned, list):
                # A hook narrows and never widens, so a careless one cannot become a permission bypass.
                approved = {id(call) for call in calls}
                calls = [call for call in returned if id(call) in approved]
        return calls

    async def after_turn(self, summary: Any) -> None:
        for hook in self._hooks:
            method = getattr(hook, "after_turn", None)
            if method is None:
                continue
            try:
                await method(summary)
            except Exception:  # noqa: BLE001
                logger.warning("%s.after_turn raised; skipping it", type(hook).__name__, exc_info=True)


__all__ = ["HookRunner", "MaximumToolCalls"]
