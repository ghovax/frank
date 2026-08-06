"""Who answers a gate."""

from __future__ import annotations

from enum import StrEnum
from typing import Optional


class PermissionMode(StrEnum):
    """Who answers when a call asks to reach past its confinement."""

    ASK = "ask"
    AUTOMATIC = "automatic"

    @classmethod
    def parse(cls, value: str | PermissionMode | None) -> Optional[PermissionMode]:
        """The mode a string names, or ``None`` when it names no known mode — so a caller can tell 'absent or invalid' apart from a real choice."""
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError:
            return None

    @classmethod
    def coerce(cls, value: str | PermissionMode | None, default: PermissionMode | None = None) -> PermissionMode:
        """The mode a value names, falling back to ``default`` (or :attr:`ASK`) when it names none."""
        parsed = cls.parse(value)
        return parsed if parsed is not None else (default if default is not None else cls.ASK)

    @property
    def restrictiveness(self) -> int:
        """Position in the restrictiveness order, least to most: ``automatic < ask``."""
        return 1 if self is PermissionMode.ASK else 0

    @classmethod
    def more_restrictive(cls, *modes: str | PermissionMode | None) -> PermissionMode:
        """The more restrictive of the given modes — a meet on the restrictiveness order."""
        candidates = [mode for mode in (cls.parse(value) for value in modes) if mode is not None]
        return max(candidates, key=lambda mode: mode.restrictiveness) if candidates else cls.ASK

    @classmethod
    def child_of(
        cls,
        parent: str | PermissionMode | None,
        *,
        requested: str | PermissionMode | None = None,
        fallback: str | PermissionMode | None = None,
        ceiling: str | PermissionMode | None = None,
    ) -> PermissionMode:
        """The mode a session created by ``parent`` runs under, or ``ValueError`` if there is none it can have."""
        parent_mode = cls.parse(parent)
        chosen = cls.more_restrictive(
            cls.parse(requested) or parent_mode or cls.parse(fallback), parent_mode, ceiling,
        )
        if parent_mode is not None and parent_mode.never_asks and not chosen.never_asks:
            raise ValueError(
                f"a session running unattended can only create sessions that also run "
                f"unattended, and {chosen} stops to ask"
            )
        return chosen

    @property
    def never_asks(self) -> bool:
        """Whether this mode can run with nobody watching."""
        return self is PermissionMode.AUTOMATIC

    @property
    def asks(self) -> bool:
        """Whether a gate goes to a person."""
        return self is PermissionMode.ASK
