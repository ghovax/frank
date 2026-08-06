"""What a tool needs from the session it is running inside."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field, replace
from typing import Any, Optional, Sequence

from frank.base import environment_variables
from frank.base.confinement import Grant, Profile


@dataclass(frozen=True)
class ToolContext:
    """The session-shaped state the tools read at call time."""

    # What every child process this call spawns is confined to, and the directory its `$WORKSPACE` resolves to.
    sandbox: Profile = field(default_factory=Profile)
    workspace: str = ""

    # Capability clients, built from the session's configuration.
    exa_client: Any = None
    mcp_manager: Any = None
    firecrawl_client: Any = None
    jina_api_key: str = ""
    proxy_url: str = ""

    # How this session reaches its peers.
    session_access: Any = None

    # Which session this is, for the children that have to say.
    session_id: str = ""

    # The tools this session installed for itself, and where they live.
    toolbox: Any = None

    # Whether this is a second run of a command the operating system refused.
    retrying: bool = False

    def child_environment(self, inherited: Optional[dict] = None) -> dict:
        """What a child process needs beyond the confinement's own environment: who it belongs to, and — when this session has a toolbox — its own tools on `PATH` with the package manager pointed at its own profile."""
        environment = {environment_variables.SESSION_ID: self.session_id} if self.session_id else {}
        if self.toolbox is not None:
            environment.update(self.toolbox.environment(inherited))
        return environment

    def with_attachments(self, paths: "Sequence[str]") -> "ToolContext":
        """This context with read access to the exact files the user attached this turn."""
        if not paths:
            return self
        return replace(self, sandbox=self.sandbox.with_attachments(paths))

    def with_grants(self, grants: "Sequence[Grant]") -> "ToolContext":
        """This context with approved widenings folded into the profile the child will run under."""
        if not grants:
            return self
        profile = self.sandbox
        for grant in grants:
            profile = profile.with_grant(grant, workspace=self.workspace)
        return replace(self, sandbox=profile)

    def for_retry(self, grant: "Grant") -> "ToolContext":
        """This context for a second run of a command the operating system refused."""
        return replace(
            self,
            sandbox=self.sandbox.with_grant(grant, workspace=self.workspace),
            retrying=True,
        )

    def for_directory(self, directory: str) -> "ToolContext":
        """This context with its workspace repointed, for a call that runs somewhere else."""
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
