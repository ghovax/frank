"""Describing an exception as fields, in one place, so every record names it the same way."""

from __future__ import annotations

import logging
from typing import Any

__all__ = ["describe", "log_fields", "summary"]

#: The attributes a record already carries, which `extra=` is forbidden to overwrite.
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


def summary(error: BaseException) -> str:
    """One line naming an exception, type included, since a bare message is often a noun that says nothing."""
    text = " ".join(str(error).split())
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def describe(error: BaseException) -> dict[str, Any]:
    """An exception as fields, with `cause` from a chained one, which is regularly the more interesting."""
    described: dict[str, Any] = {"error": type(error).__name__, "message": " ".join(str(error).split())}
    cause = error.__cause__ or error.__context__
    if cause is not None:
        described["cause"] = summary(cause)
    return described


def log_fields(error: BaseException | None = None, /, **context: Any) -> dict[str, Any]:
    """The same fields as metadata on a record, with any name a record owns prefixed rather than refused."""
    fields: dict[str, Any] = dict(context)
    if error is not None:
        fields.update(describe(error))
    return {(f"detail_{name}" if name in _RESERVED else name): value for name, value in fields.items()}
