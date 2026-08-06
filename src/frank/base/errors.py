"""Describing an exception as fields, in one place.

The interface needed a library for this — JavaScript lets you throw a string, so a caught value
there may have no `message` at all and `serialize-error` is what makes one. Python has the
opposite property: `raise` only accepts a `BaseException`, so anything caught already carries a
type, arguments and a traceback. The parsing library is `traceback`, in the standard library,
and adding a third-party one would be a dependency for something the language gives.

What was missing is not parsing but *agreement*. Six places wrote `f"{type(error).__name__}:
{error}"` by hand and a dozen more wrote `str(error)`, so the same exception was described three
ways depending on which file caught it. These two functions are that description, once.

**Where each belongs.** `summary` is one line for a log field or a message crossing a process
boundary — no newlines, so it cannot break a line-oriented log or a queue payload. `describe`
is the same thing as a dict, for a structured record. Neither carries the traceback: that
belongs to `logger.exception`, in the process that caught it, where the frames still refer to
code rather than to a string that was shipped somewhere else.
"""

from __future__ import annotations

from typing import Any

__all__ = ["describe", "summary"]


def summary(error: BaseException) -> str:
    """One line naming an exception: ``ValueError: the port was already bound``.

    The type is included because an exception's message on its own is often a bare noun —
    ``"parakeet_mlx"`` from a `ModuleNotFoundError` says nothing without it. Newlines are
    collapsed so this stays one field in a log line.
    """
    text = " ".join(str(error).split())
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def describe(error: BaseException) -> dict[str, Any]:
    """An exception as fields, for a structured record.

    `cause` is the chained exception's summary when there is one — a `raise … from …` links two
    exceptions, and the one that was raised is regularly the less interesting of the pair.
    """
    described: dict[str, Any] = {"error": type(error).__name__, "message": " ".join(str(error).split())}
    cause = error.__cause__ or error.__context__
    if cause is not None:
        described["cause"] = summary(cause)
    return described
