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
policy available is :attr:`SELF_CLASSIFY`, which still classifies every call and escalates
anything it cannot prove safe.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Optional


class PermissionMode(StrEnum):
    """The policy governing a session's tool calls. A :class:`~enum.StrEnum`, so it is equal
    to (and serializes as) its wire/config/database string and no boundary special-cases it.

    - ``DEFAULT`` — interactive: per-command rules; an unmatched command asks the user.
    - ``PERMISSIVE`` — an unmatched command runs; anything the model called medium or high
      risk asks. No classifier, so no model call and no judgement beyond the two facts the
      barrier already has.
    - ``SELF_CLASSIFY`` — a classifier judges what the barrier could not settle: it approves
      provably-safe calls and escalates the rest.
    - ``READ_ONLY`` — every write is hard-blocked (investigation sessions).

    ``PERMISSIVE`` exists because the gap between the first two was too wide to live in.
    ``DEFAULT`` asks about every command its rules do not name, which on a real machine is most
    of them; ``SELF_CLASSIFY`` stops asking about the ones a classifier can vouch for, at the cost of a
    model call per ambiguous call and of trusting that judgement. What was missing was the
    obvious middle: believe the risk the model already declared, run what it called low, and
    ask about the rest. Between the two in restrictiveness, and cheaper than either to reason
    about — no rule you wrote is ignored, and nothing is approved by a second model.
    """

    DEFAULT = "default"
    PERMISSIVE = "permissive"
    SELF_CLASSIFY = "self_classify"
    READ_ONLY = "read_only"

    @classmethod
    def parse(cls, value: str | PermissionMode | None) -> Optional[PermissionMode]:
        """The mode a string names, or ``None`` when it names no known mode — so a caller can
        tell 'absent or invalid' apart from a real choice. A stored ``bypass`` from before
        that mode was removed parses as ``None`` and therefore falls back to the interactive
        default rather than silently granting the loosest policy.

        A stored ``auto`` is different in kind and is translated rather than dropped: that mode
        was not removed, it was renamed. ``auto`` said how the decision arrived and not who made
        it, which is the part worth knowing — a second model classifies the call. The policy is
        unchanged, so reading the old spelling as the new one is finishing the rename in data
        somebody wrote before it, not reviving a mode. Dropping it instead would silently move a
        session that had chosen the loosest policy to the strictest one that still works."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str) and value in _RENAMED:
            return _RENAMED[value]
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
        ``self_classify < permissive < default < read_only``."""
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
    def is_self_classifying(self) -> bool:
        """Whether the harness asks a model to judge a call the barrier could not settle."""
        return self is PermissionMode.SELF_CLASSIFY

    @property
    def is_interactive(self) -> bool:
        """The manual ('ask') policy: a command no rule names is asked about rather than run.

        This is the one thing ``PERMISSIVE`` changes, and the whole of what it changes. Every
        other difference between the modes falls out of these three flags being false together:
        not interactive, so an unmatched command is allowed; not auto, so no classifier is
        consulted and the declared risk stands; not read-only, so writes are not blocked. The
        result is exactly "run what the model called low-risk, ask about the rest"."""
        return self is PermissionMode.DEFAULT


# Spellings that named a mode that still exists. Read on the way in and never written, so the
# old name disappears from anything this touches the first time it is saved.
_RENAMED: dict[str, PermissionMode] = {"auto": PermissionMode.SELF_CLASSIFY}


_RESTRICTIVENESS: dict[PermissionMode, int] = {
    PermissionMode.SELF_CLASSIFY: 0,
    # Above `auto` because it escalates everything the model called medium or high, where `auto`
    # gives the classifier a chance to vouch for it; below `default` because it runs what
    # `default` would have asked about. The clamp reads this, so a child of a `permissive`
    # parent may be `permissive`, `default` or `read_only`, and never `auto`.
    PermissionMode.PERMISSIVE: 1,
    PermissionMode.DEFAULT: 2,
    PermissionMode.READ_ONLY: 3,
}
