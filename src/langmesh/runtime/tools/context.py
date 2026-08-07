"""What a tool needs from its session, bound per task so two turns in one worker cannot cross."""

from __future__ import annotations

import contextvars
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from langmesh.base import environment_variables
from langmesh.base.confinement import Grant, Profile


@dataclass(frozen=True)
class ToolContext:
    """The session-shaped state tools read at call time. Frozen, so narrowing produces a new value."""

    # What every child is confined to, and the directory `$WORKSPACE` resolves to.
    sandbox: Profile = field(default_factory=Profile)
    workspace: str = ""

    # Capability clients. Absent means unconfigured, which each tool reports in its own words.
    exa_client: Any = None
    mcp_manager: Any = None
    firecrawl_client: Any = None
    jina_api_key: str = ""
    proxy_url: str = ""

    # What a fetch is given and what it must return to count, as the person configured them.
    fetch_timeout_seconds: int = 30
    download_timeout_seconds: int = 120
    minimum_useful_characters: int = 64

    # How this session reaches its peers, supplied by the worker; the runtime holds no identity.
    session_access: Any = None
    conversation_snapshot: Optional[Callable[[], list[dict[str, Any]]]] = None

    # Which session this is, so a child reaching for the CLI creates a child of it rather than an orphan.
    session_id: str = ""

    # The tools this session installed. `None` means none offered, not a degraded one.
    toolbox: Any = None

    # Whether this is a second run: the only thing that can make a command run wider than its session.
    retrying: bool = False

    def spill_path(self, prefix: str) -> "Path":
        """Where a tool's overflow output lands: somewhere this profile permits, never in the tree being worked in."""
        from pathlib import Path
        from langmesh.base import confinement as _confinement
        from langmesh.base.identifiers import new_id

        scratch = _confinement.temporary_directory(self.sandbox, workspace=self.workspace)
        return Path(scratch or tempfile.gettempdir()) / f"{new_id(prefix)}.log"

    def child_environment(self, inherited: Optional[dict] = None) -> dict:
        """What a child needs beyond the confinement's environment: who it belongs to, and its toolbox."""
        environment = {environment_variables.SESSION_ID: self.session_id} if self.session_id else {}
        if self.toolbox is not None:
            environment.update(self.toolbox.environment(inherited))
        return environment

    def with_attachments(self, paths: "Sequence[str]") -> "ToolContext":
        """This context with read access to the files attached this turn, derived rather than assigned."""
        if not paths:
            return self
        return replace(self, sandbox=self.sandbox.with_attachments(paths))

    def with_grants(self, grants: "Sequence[Grant]") -> "ToolContext":
        """This context with standing grants folded in, leaving `sandbox` as what a person configured."""
        if not grants:
            return self
        profile = self.sandbox
        for grant in grants:
            profile = profile.with_grant(grant, workspace=self.workspace)
        return replace(self, sandbox=profile)

    def for_retry(self, grant: "Grant") -> "ToolContext":
        """This context for one re-run of a refused command, which lives only as long as the binding."""
        return replace(
            self,
            sandbox=self.sandbox.with_grant(grant, workspace=self.workspace),
            retrying=True,
        )

    def for_directory(self, directory: str) -> "ToolContext":
        """This context with its workspace repointed, as a new value rather than a mutation."""
        return replace(self, workspace=directory)


_EMPTY = ToolContext()

_current: contextvars.ContextVar[Optional[ToolContext]] = contextvars.ContextVar(
    "langmesh_tool_context", default=None
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
