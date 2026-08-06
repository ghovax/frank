"""Value types for resolving a call's location and carrying its execution policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from langmesh.base.permission_mode import PermissionMode
from langmesh.locations.executor import LocationExecutor


@dataclass
class ResolvedLocation:
    """A location resolved for execution: its identity, its executor, its base directory, its mode."""

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
    """One call's execution policy, threaded as a value so concurrent calls cannot cross locations."""

    location: ResolvedLocation | None
    working_directory: str
    mode: PermissionMode

    @property
    def asks(self) -> bool:
        """Whether a gate raised by this call goes to a person rather than the reviewer."""
        return self.mode.asks

    @property
    def is_remote(self) -> bool:
        return self.location is not None and self.location.is_remote


# The tools that act on a location's filesystem or shell, and so resolve against one.
_LOCATION_TOOLS = frozenset({"bash", "read_file", "write_file", "edit_file", "search_code", "download_file"})


class PermissionDecision(BaseModel):
    """The reviewer's verdict. Its ``risk`` is its own reading, which the agent cannot see and did not supply."""

    action: Literal["allow", "deny"]
    explanation: str
    risk: Literal["low", "medium", "high"]
