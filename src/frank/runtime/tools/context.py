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
from typing import Any, Optional, Sequence

from frank.base import environment_variables
from frank.base.confinement import Grant, Profile


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

    # Which session this is, for the children that have to say. A shell command that reaches for
    # the `frank` CLI creates a *child* of this session rather than an orphan, and it can only do
    # that if it is told who its parent is. It arrives here rather than being read back out of
    # the worker's own environment, which is the pattern this whole module exists to end: a
    # process global is process-wide, and a worker legitimately runs two turns at once.
    session_id: str = ""

    # The tools this session installed for itself, and where they live. `None` on a machine that
    # cannot offer one, or when the person turned it off — and `None` is not a degraded toolbox:
    # nothing is put on `PATH` and the agent is told nothing about installing anything.
    toolbox: Any = None

    # Whether this is a second run of a command the operating system refused. The registry reads
    # it to build the right kind of attempt: a first attempt takes no grant and cannot, so this
    # flag is the only thing that can make a command run wider than its session.
    retrying: bool = False

    def child_environment(self, inherited: Optional[dict] = None) -> dict:
        """What a child process needs beyond the confinement's own environment: who it belongs
        to, and — when this session has a toolbox — its own tools on `PATH` with the package
        manager pointed at its own profile.

        Passed explicitly because the confinement does not inherit: a child's environment is
        built from an allowlist, so anything the harness wants a child to know has to be handed
        over by name."""
        environment = {environment_variables.SESSION_ID: self.session_id} if self.session_id else {}
        if self.toolbox is not None:
            environment.update(self.toolbox.environment(inherited))
        return environment

    def with_attachments(self, paths: "Sequence[str]") -> "ToolContext":
        """This context with read access to the exact files the user attached this turn.

        Derived like everything else here, so one turn's attachment cannot widen a compaction
        or an autonomous wake running beside it in the same worker.
        """
        if not paths:
            return self
        return replace(self, sandbox=self.sandbox.with_attachments(paths))

    def with_grants(self, grants: "Sequence[Grant]") -> "ToolContext":
        """This context with approved widenings folded into the profile the child will run under.

        Derived rather than assigned, for exactly the reason :meth:`for_directory` is. The
        confinement a tool reads is per-task, and a grant approved in one turn must not silently
        widen a compaction or an autonomous wake running beside it in the same worker.

        Applied here rather than at the profile's source so that `sandbox` stays what a person
        configured. Two turns can then be asked the same question — what may this session reach —
        and get the standing answer and the granted one apart from each other.
        """
        if not grants:
            return self
        profile = self.sandbox
        for grant in grants:
            profile = profile.with_grant(grant, workspace=self.workspace)
        return replace(self, sandbox=profile)

    def for_retry(self, grant: "Grant") -> "ToolContext":
        """This context for a second run of a command the operating system refused.

        Separate from :meth:`with_grants` because the two have different lifetimes: a session's
        grants stand until it ends, and this one covers one re-run of one command and is gone
        with the context it was bound into."""
        return replace(
            self,
            sandbox=self.sandbox.with_grant(grant, workspace=self.workspace),
            retrying=True,
        )

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
