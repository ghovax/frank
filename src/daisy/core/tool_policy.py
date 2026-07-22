"""Value types for tool-call location resolution and permission policy.

A tool call may target a project *location* (the local machine or a configured SSH
remote); these types capture the resolved location, the per-call execution policy threaded
through as an immutable value (so concurrent calls never cross working directories or
permission flags), and the structured decisions the bash permission classifier emits.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Optional

from pydantic import BaseModel

from daisy.locations.executor import LocationExecutor


class PermissionMode(StrEnum):
    """The permission policy governing a turn or a single tool call — the one typed value
    that replaces the mode strings and the ``(read_only, bypass, auto)`` boolean trio the
    runtime used to carry as three parallel fields. A :class:`~enum.StrEnum`, so it is equal
    to (and serializes as) its wire/config/DB string, and no boundary needs to special-case it.

    - ``DEFAULT`` — interactive: per-command rules; an unmatched command asks the user.
    - ``AUTO`` — an LLM classifier auto-approves provably-safe calls and escalates the rest.
    - ``READ_ONLY`` — every write is hard-blocked (investigation agents).
    - ``BYPASS`` — everything runs with no gate (never applied to a delegated agent).
    """

    DEFAULT = "default"
    AUTO = "auto"
    READ_ONLY = "read_only"
    BYPASS = "bypass"

    @classmethod
    def parse(cls, value: str | PermissionMode | None) -> Optional[PermissionMode]:
        """The mode a wire/config/DB string names, or ``None`` when it names no known mode —
        so a caller can tell 'absent or invalid' apart from a real choice."""
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError:
            return None

    @classmethod
    def coerce(cls, value: str | PermissionMode | None, default: PermissionMode | None = None) -> PermissionMode:
        """The mode a value names, falling back to ``default`` (or :attr:`DEFAULT`) when it
        names none. The typed parse to apply at a string boundary."""
        parsed = cls.parse(value)
        return parsed if parsed is not None else (default if default is not None else cls.DEFAULT)

    @property
    def restrictiveness(self) -> int:
        """Position in the single linear restrictiveness order the harness has always used,
        least to most: ``bypass < auto < default < read_only``. (The two-axis refinement is a
        deliberate open question in the typed-turn-core plan, not today's behavior.)"""
        return _RESTRICTIVENESS[self]

    @classmethod
    def more_restrictive(cls, *modes: str | PermissionMode | None) -> PermissionMode:
        """The more restrictive of the given modes — a lattice meet on the restrictiveness
        order. Unknown/absent inputs are ignored; with none given the interactive default
        applies. This is the delegated grant clamp: an agent runs at the more restrictive of
        its caller's grant and its own card."""
        candidates = [mode for mode in (cls.parse(value) for value in modes) if mode is not None]
        return max(candidates, key=lambda mode: mode.restrictiveness) if candidates else cls.DEFAULT

    def clamped_for_delegation(self) -> PermissionMode:
        """This mode as a delegated agent may run it: :attr:`BYPASS` is clamped to the
        interactive default, because a delegated agent never runs unattended."""
        return PermissionMode.DEFAULT if self is PermissionMode.BYPASS else self

    # One-hot views: the many existing call sites keep reading a simple boolean, but off this
    # single source of truth rather than three fields that could drift out of sync.
    @property
    def is_read_only(self) -> bool:
        return self is PermissionMode.READ_ONLY

    @property
    def is_bypass(self) -> bool:
        return self is PermissionMode.BYPASS

    @property
    def is_auto(self) -> bool:
        return self is PermissionMode.AUTO

    @property
    def is_interactive(self) -> bool:
        """The manual ('ask') policy: not auto-classifying, not read-only, not bypass."""
        return self is PermissionMode.DEFAULT


_RESTRICTIVENESS: dict[PermissionMode, int] = {
    PermissionMode.BYPASS: 0,
    PermissionMode.AUTO: 1,
    PermissionMode.DEFAULT: 2,
    PermissionMode.READ_ONLY: 3,
}


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
    def bypass_permissions(self) -> bool:
        return self.mode.is_bypass

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
