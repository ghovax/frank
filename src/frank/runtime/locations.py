"""Value types for tool-call location resolution and permission decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from frank.base.permission_mode import PermissionMode
from frank.locations.executor import LocationExecutor


@dataclass
class ResolvedLocation:
    """A workspace location resolved for execution: its identity (uri/name), the executor that runs tools against it (local subprocess or multiplexed SSH), the base directory tools treat as cwd, and its effective execution policy."""

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
    """The effective execution policy for ONE tool call: the resolved location it targets (``None`` for tools that do not address a location), the directory its shell/file work runs in, and the permission flags in force."""

    location: ResolvedLocation | None
    working_directory: str
    mode: PermissionMode

    @property
    def asks(self) -> bool:
        """Whether a gate raised by this call goes to a person."""
        return self.mode.asks

    @property
    def is_remote(self) -> bool:
        return self.location is not None and self.location.is_remote


# The tools that operate on a location's filesystem/shell and therefore resolve against a location (``search_code`` indexes the location's root; it is local-only, so a remote root simply yields no results).
_LOCATION_TOOLS = frozenset({"bash", "read_file", "write_file", "edit_file", "search_code", "download_file"})


class PermissionDecision(BaseModel):
    """What the reviewer decided about one request to reach past the confinement."""

    action: Literal["allow", "deny"]
    explanation: str
    risk: Literal["low", "medium", "high"]
