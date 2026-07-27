"""What a tool needs from the session it is running inside.

The tools are module-level singletons — one `StructuredTool` per verb, built once at import
and invoked by every runtime — so anything they need from their session has to reach them
some other way. That used to be eight module globals and eight setters, installed once by the
worker at startup, which worked only because a worker serves exactly one session: process
scope happened to be session scope.

It stopped being merely untidy at :mod:`frank.runtime.tools.dispatch`, where a `bash` call
naming its own working directory rewrote the process-wide confinement profile mid-turn. A
worker can legitimately have two turns open at once — a compaction or an autonomous wake
alongside the user's — so one turn could narrow another turn's sandbox to its own directory.

A context variable is the fix, and it is the pattern this package already uses:
:mod:`frank.runtime.background` binds the active job runner exactly this way. The value is per
task rather than per process, so two turns in one interpreter each see their own, and a
derived context (a `bash` call that runs somewhere else) narrows one call without touching
anything else.

Nothing here is optional-by-accident. :func:`current` answers with an empty context rather
than raising, because a tool invoked outside a runtime — a test, a REPL — should report that
its capability is unconfigured in its own words rather than blowing up inside the harness.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from frank.base.confinement import Profile


@dataclass(frozen=True)
class ToolContext:
    """The session-shaped state the tools read at call time.

    Frozen, so a tool cannot reach back and mutate what the runtime handed it, and narrowing
    is :meth:`for_directory` producing a new value rather than an assignment nobody can see.
    """

    # What every child process this call spawns is confined to, and the directory its
    # `$WORKSPACE` resolves to. Resolved by the daemon at session creation and clamped there.
    sandbox: Profile = field(default_factory=Profile)
    workspace: str = ""

    # Capability clients, built from the session's configuration. Absent means the user has
    # not configured that provider, which each tool reports in its own words.
    exa_client: Any = None
    mcp_manager: Any = None
    firecrawl_client: Any = None
    jina_api_key: str = ""
    proxy_url: str = ""

    # How this session reaches its peers. Supplied by the worker, which is the layer that
    # knows which session this is; the runtime deliberately carries no identity of its own.
    session_access: Any = None

    def for_directory(self, directory: str) -> "ToolContext":
        """This context with its workspace repointed, for a call that runs somewhere else.

        A new value rather than a mutation: the old code rewrote a module global here, which
        meant a concurrent turn's `bash` inherited a sandbox narrowed to a directory it had
        never heard of.
        """
        return replace(self, workspace=directory)


_EMPTY = ToolContext()

_current: contextvars.ContextVar[Optional[ToolContext]] = contextvars.ContextVar(
    "frank_tool_context", default=None
)


def bind(context: ToolContext) -> contextvars.Token:
    """Make `context` the one tools see, for this task. Pair with :func:`unbind`."""
    return _current.set(context)


def unbind(token: contextvars.Token) -> None:
    _current.reset(token)


def current() -> ToolContext:
    """The bound context, or an empty one outside a runtime."""
    return _current.get() or _EMPTY


__all__ = ["ToolContext", "bind", "current", "unbind"]
