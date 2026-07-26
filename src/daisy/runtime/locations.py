"""Value types for tool-call location resolution and permission decisions.

A tool call may target a project *location* (the local machine or a configured SSH
remote); these types capture the resolved location, the per-call execution policy threaded
through as an immutable value (so concurrent calls never cross working directories or
permission flags), and the structured decisions the bash permission classifier emits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from daisy.base.permission_mode import PermissionMode
from daisy.locations.executor import LocationExecutor


@dataclass
class ResolvedLocation:
    """A project location resolved for execution: its identity (uri/name), the executor
    that runs tools against it (local subprocess or multiplexed SSH), the base directory
    tools treat as cwd, and its effective execution policy."""

    uri: str
    name: str
    kind: str  # "local" | "remote"
    base_directory: str
    executor: LocationExecutor
    permission_mode: PermissionMode

    @property
    def is_remote(self) -> bool:
        return self.kind == "remote"


class ToolLocationError(ValueError):
    """A tool call named a `location` that is missing, ambiguous, or unknown."""


@dataclass(frozen=True)
class CallExecutionPolicy:
    """The effective execution policy for ONE tool call: the resolved location it
    targets (``None`` for tools that do not address a location), the directory its
    shell/file work runs in, and the permission flags in force. Computed per call and
    threaded through as a value — never written to runtime state — so tool calls
    running concurrently against different locations cannot cross policies or
    working directories."""

    location: ResolvedLocation | None
    working_directory: str
    mode: PermissionMode

    # The permission flags every tool call consults, derived from the one resolved mode so
    # they can never disagree. Kept as properties because the read sites are many and simple.
    @property
    def read_only(self) -> bool:
        return self.mode.is_read_only


    @property
    def auto_permissions(self) -> bool:
        return self.mode.is_auto

    @property
    def is_remote(self) -> bool:
        return self.location is not None and self.location.is_remote


# The tools that operate on a location's filesystem/shell and therefore resolve against a location
# (``search_code`` indexes the location's root; it is local-only, so a remote root simply yields no
# results).
_LOCATION_TOOLS = frozenset({"bash", "read_file", "write_file", "edit_file", "search_code"})


class BashAllowRule(BaseModel):
    """Structured output for an 'always allow' rule: the command pattern(s) to
    auto-allow for the rest of the session (e.g. ``["cat *", "ls *"]``)."""
    patterns: list[str]


class PermissionDecision(BaseModel):
    """Structured decision for automatic permission classification."""

    action: Literal["auto_approve", "escalate"]
    justification: str
    risk: Literal["low", "medium", "high"]
