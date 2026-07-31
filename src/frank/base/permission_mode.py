"""The permission policy a session runs under.

A session's mode is chosen at ``create`` and can be changed afterwards by the person running
it, through ``session.permission_mode`` on the control plane — a conversation that earns trust
should not have to be restarted to stop being asked about every command, and one that has lost
it should not have to be ended to be reined in. The change reaches the turn already in flight,
because every decision reads the mode at call time.

Two clamps survive that, and they are what the old immobility was really protecting. A child
session is never looser than its parent, which :meth:`PermissionMode.more_restrictive` computes
as a meet on the restrictiveness order; and no session is ever looser than its agent profile's
own ceiling. Tightening a session tightens the subtree it created, so authority a session gives
up cannot live on in one of its children. A *session* cannot make this call at all — it is
absent from the verbs a session token may use — so widening is the human's act, never the
model's.

There is deliberately **no bypass mode**. An agent that runs with no gate at all is the one
configuration whose blast radius is unbounded, and sessions now spawn sessions without a
human in the loop, so the mode that disables the loop entirely is not offered. The loosest
policy available is :attr:`AUTO`, which still classifies every call and escalates anything
it cannot prove safe.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Optional


class PermissionMode(StrEnum):
    """The policy governing a session's tool calls. A :class:`~enum.StrEnum`, so it is equal
    to (and serializes as) its wire/config/database string and no boundary special-cases it.

    - ``DEFAULT`` — interactive: per-command rules; an unmatched command asks the user.
    - ``AUTO`` — a classifier auto-approves provably-safe calls and escalates the rest.
    - ``READ_ONLY`` — every write is hard-blocked (investigation sessions).
    """

    DEFAULT = "default"
    AUTO = "auto"
    READ_ONLY = "read_only"

    @classmethod
    def parse(cls, value: str | PermissionMode | None) -> Optional[PermissionMode]:
        """The mode a string names, or ``None`` when it names no known mode — so a caller can
        tell 'absent or invalid' apart from a real choice. A stored ``bypass`` from before
        that mode was removed parses as ``None`` and therefore falls back to the interactive
        default rather than silently granting the loosest policy."""
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
        """Position in the linear restrictiveness order, least to most:
        ``auto < default < read_only``."""
        return _RESTRICTIVENESS[self]

    @classmethod
    def more_restrictive(cls, *modes: str | PermissionMode | None) -> PermissionMode:
        """The more restrictive of the given modes — a meet on the restrictiveness order.
        Unknown or absent inputs are ignored; with none given the interactive default applies.
        This is the child-session clamp: a spawned session runs at the more restrictive of its
        parent's mode and the mode its creator asked for, so a child can never be looser than
        the session that spawned it."""
        candidates = [mode for mode in (cls.parse(value) for value in modes) if mode is not None]
        return max(candidates, key=lambda mode: mode.restrictiveness) if candidates else cls.DEFAULT

    # One-hot views: call sites read a simple boolean, but off this single source of truth
    # rather than parallel fields that could drift out of sync.
    @property
    def is_read_only(self) -> bool:
        return self is PermissionMode.READ_ONLY

    @property
    def is_auto(self) -> bool:
        return self is PermissionMode.AUTO

    @property
    def is_interactive(self) -> bool:
        """The manual ('ask') policy: not auto-classifying and not read-only."""
        return self is PermissionMode.DEFAULT


_RESTRICTIVENESS: dict[PermissionMode, int] = {
    PermissionMode.AUTO: 0,
    PermissionMode.DEFAULT: 1,
    PermissionMode.READ_ONLY: 2,
}
