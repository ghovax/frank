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
configuration whose blast radius is unbounded, and sessions now create sessions without a
human in the loop, so the mode that disables the loop entirely is not offered. The loosest
policy available is :attr:`CLASSIFY`, which judges every call it is given and answers for
itself — but it does answer, and a call it cannot vouch for is refused rather than run.
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
    - ``CLASSIFY`` — a classifier settles what the barrier could not, and **never asks**: it
      allows the call or it denies it.
    - ``READ_ONLY`` — every write is hard-blocked (investigation sessions).

    Only the first two ever put a question in front of a person, and that is the line between
    them. ``DEFAULT`` and ``PERMISSIVE`` differ in how much they ask; ``CLASSIFY`` and
    ``READ_ONLY`` differ in how they decide, and neither of them asks at all.

    ``PERMISSIVE`` exists because the gap between the two interactive ends was too wide to live
    in. ``DEFAULT`` asks about every command its rules do not name, which on a real machine is
    most of them. What was missing was the obvious middle: believe the risk the model already
    declared, run what it called low, and ask about the rest.

    ``CLASSIFY`` is for work nobody is watching. An agent sent off to do a job cannot be
    autonomous and also stop every few minutes for a click — a mode that escalates is a mode
    that needs somebody at the keyboard, which is the opposite of what it is for. So the
    classifier is given the whole decision: it allows what it can vouch for and denies the
    rest, and a denial reaches the model as a refusal it can work around rather than a prompt
    nobody is there to answer. The protection is that the judge still runs on every ambiguous
    call, and still fails closed.
    """

    DEFAULT = "default"
    PERMISSIVE = "permissive"
    CLASSIFY = "classify"
    READ_ONLY = "read_only"

    @classmethod
    def parse(cls, value: str | PermissionMode | None) -> Optional[PermissionMode]:
        """The mode a string names, or ``None`` when it names no known mode — so a caller can
        tell 'absent or invalid' apart from a real choice.

        The four spellings above are the whole of it. A name this does not know — ``bypass``
        from before that mode was removed, ``self_classify`` from when the mode was named for
        who judges rather than for what it does — is not a mode, and reading it as one would
        mean two spellings for a policy and configuration files that disagree about which is
        real. It falls back to the interactive default, which is the safe end of the order and
        the one a person can see being applied."""
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
        ``classify < permissive < default < read_only``."""
        return _RESTRICTIVENESS[self]

    @classmethod
    def more_restrictive(cls, *modes: str | PermissionMode | None) -> PermissionMode:
        """The more restrictive of the given modes — a meet on the restrictiveness order.
        Unknown or absent inputs are ignored; with none given the interactive default applies.
        This is the child-session clamp: a created session runs at the more restrictive of its
        parent's mode and the mode its creator asked for, so a child can never be looser than
        the session that created it."""
        candidates = [mode for mode in (cls.parse(value) for value in modes) if mode is not None]
        return max(candidates, key=lambda mode: mode.restrictiveness) if candidates else cls.DEFAULT

    @property
    def never_asks(self) -> bool:
        """Whether this mode can run with nobody watching.

        Only :attr:`CLASSIFY`. `read_only` looks like it belongs here and does not: it still
        stops for a read outside the working directory and for a request to widen access, so a
        read-only session left alone parks on the first of either."""
        return self is PermissionMode.CLASSIFY

    @classmethod
    def child_of(
        cls,
        parent: str | PermissionMode | None,
        *,
        requested: str | PermissionMode | None = None,
        fallback: str | PermissionMode | None = None,
        ceiling: str | PermissionMode | None = None,
    ) -> PermissionMode:
        """The mode a session created by ``parent`` runs under, or ``ValueError`` if there is
        none it can have.

        Three rules, in one place because they interact and a caller applying two of them is a
        caller with a hole:

        1. **Absent means inherit.** A peer created without a mode works the way its creator
           works. Falling back to the machine default instead meant an unattended session could
           delegate to a peer that stops and asks.
        2. **Never looser than the parent**, and never looser than the agent profile's own
           ceiling. This is the clamp, and it is what makes a session's authority something you
           can reason about from the outside.
        3. **A child of a session that cannot ask, cannot ask either.** Nobody is watching the
           parent, so nobody is watching the child: a gate it raises reaches no one, it waits
           forever, and the parent waits on it. Note this cannot be satisfied by *tightening* —
           `read_only` is more restrictive and still asks — so it is a genuine constraint and
           not a clamp, and where a profile's ceiling forbids it there is no legal answer.
        """
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

    # One-hot views: call sites read a simple boolean, but off this single source of truth
    # rather than parallel fields that could drift out of sync.
    @property
    def is_read_only(self) -> bool:
        return self is PermissionMode.READ_ONLY

    @property
    def is_classifying(self) -> bool:
        """Whether a classifier decides the calls the barrier could not settle — the whole
        decision, allow or deny, with nothing escalated to a person."""
        return self is PermissionMode.CLASSIFY

    @property
    def is_interactive(self) -> bool:
        """The manual ('ask') policy: a command no rule names is asked about rather than run.

        This is the one thing ``PERMISSIVE`` changes, and the whole of what it changes. Every
        other difference between the modes falls out of these three flags being false together:
        not interactive, so an unmatched command is allowed; not classifying, so no classifier
        is consulted and the declared risk stands; not read-only, so writes are not blocked. The
        result is exactly "run what the model called low-risk, ask about the rest"."""
        return self is PermissionMode.DEFAULT


_RESTRICTIVENESS: dict[PermissionMode, int] = {
    # Least restrictive because it is the only mode that can let a call through with nobody
    # watching. That is the axis the clamp cares about: what a session may do without a person.
    PermissionMode.CLASSIFY: 0,
    # Above `classify` because it escalates everything the model called medium or high, where
    # `classify` lets its judge vouch for it; below `default` because it runs what `default`
    # would have asked about. The clamp reads this, so a child of a `permissive` parent may be
    # `permissive`, `default` or `read_only`, and never `classify`.
    PermissionMode.PERMISSIVE: 1,
    PermissionMode.DEFAULT: 2,
    PermissionMode.READ_ONLY: 3,
}
