"""The words the fork protocol uses, shared by everyone who speaks it.

Three processes exchange these: the daemon asks for a session, the prototype forks it, and
the session itself reports whether it came up. A failure crosses two boundaries — a pipe and
a socket — and on the far side it is matched, logged and turned into a message for a person.

Which is why a reason is a **code, not a sentence**. A sentence cannot be compared without
guessing at its wording, cannot be translated, and drifts the moment someone improves the
phrasing at one end and not the other. The prose belongs at the edge, next to the person
reading it; the wire carries the name of what happened.

`describe` is that edge, and the only place the two are tied together.
"""

from __future__ import annotations

from enum import StrEnum


class StartFailure(StrEnum):
    """Why a session did not come up. Carried verbatim on the wire."""

    ASSIGNMENT_UNREADABLE = "assignment_unreadable"
    """The child could not read the assignment it was handed."""

    ASSIGNMENT_UNWRITABLE = "assignment_unwritable"
    """The prototype could not write the assignment into the pipe."""

    FORK_FAILED = "fork_failed"
    """`fork(2)` itself failed."""

    EXITED_BEFORE_SERVING = "exited_before_serving"
    """The process ended before it reported itself ready."""

    NEVER_REPORTED = "never_reported"
    """The process is alive but did not report ready inside the start timeout."""

    UNKNOWN = "unknown"
    """A reason that predates this vocabulary, or one that arrived malformed."""


_PROSE: dict[StartFailure, str] = {
    StartFailure.ASSIGNMENT_UNREADABLE: "the session could not read its assignment",
    StartFailure.ASSIGNMENT_UNWRITABLE: "the assignment could not be handed to the session",
    StartFailure.FORK_FAILED: "the session process could not be created",
    StartFailure.EXITED_BEFORE_SERVING: "the session process ended before it was serving",
    StartFailure.NEVER_REPORTED: "the session did not report ready in time",
    StartFailure.UNKNOWN: "the session did not start",
}


def describe(reason: str, detail: str = "") -> str:
    """Turn a wire reason into a sentence, keeping any detail that came with it.

    Unknown codes are passed through rather than swallowed: a reason this build does not
    recognise is still better in the log than "unknown", which is what made a mismatched pair
    of builds impossible to diagnose.
    """
    try:
        sentence = _PROSE[StartFailure(reason)]
    except ValueError:
        sentence = f"the session did not start ({reason})" if reason else _PROSE[StartFailure.UNKNOWN]
    return f"{sentence}: {detail}" if detail else sentence
