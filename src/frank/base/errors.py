"""Describing an exception as fields, in one place."""

from __future__ import annotations

from typing import Any

__all__ = ["describe", "summary"]


def summary(error: BaseException) -> str:
    """One line naming an exception: ``ValueError: the port was already bound``."""
    text = " ".join(str(error).split())
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def describe(error: BaseException) -> dict[str, Any]:
    """An exception as fields, for a structured record."""
    described: dict[str, Any] = {"error": type(error).__name__, "message": " ".join(str(error).split())}
    cause = error.__cause__ or error.__context__
    if cause is not None:
        described["cause"] = summary(cause)
    return described
