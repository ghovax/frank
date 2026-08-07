"""The one model failure the harness causes, and can therefore recognise without being told."""

from __future__ import annotations

from typing import Optional


class ContextWindowExceeded(Exception):
    """The request was larger than the model would accept, carrying which model and by how much."""

    def __init__(
        self,
        message: str,
        *,
        model: str = "",
        context_window: int = 0,
        tokens: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.model = model
        self.context_window = context_window
        self.tokens = tokens


# What a provider calls this failure in its machine-readable code, which outlasts its prose.
CONTEXT_OVERFLOW_CODES = frozenset(
    {
        "context_length_exceeded",
        "context_length_error",
        "input_too_large",
        "string_above_max_length",
        "request_too_large",
    }
)


def over_context_window(tokens: int, context_window: int) -> bool:
    """Whether a request of `tokens` cannot fit a window, compared strictly because the count is a proxy."""
    return context_window > 0 and tokens > context_window
