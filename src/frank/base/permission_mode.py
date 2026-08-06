"""Who answers a gate.

A session's mode is chosen at ``create`` and can be changed afterwards by the person running
it, through ``session.permission_mode`` on the control plane — a conversation that earns trust
should not have to be restarted to stop being asked about every command, and one that has lost
it should not have to be ended to be reined in. The change reaches the turn already in flight,
because every decision reads the mode at call time.

**This enum says who decides, and nothing about what is allowed.** What a session may do is its
confinement, configured under ``sandbox`` and enforced by the operating system — a session that
may not write is one whose profile has nowhere writable, not one whose commands are matched
against a list of verbs that might mean writing.

So this answers the one question a policy is for: when a call wants to reach past its
confinement, who says yes. A person, or the reviewer.

Two clamps hold. A child session is never looser than its parent, which
:meth:`PermissionMode.more_restrictive` computes as a meet on the restrictiveness order; and no
session is ever looser than its agent profile's own ceiling. Tightening a session tightens the
subtree it created, so authority a session gives up cannot live on in one of its children. A
*session* cannot make this call at all — it is absent from the verbs a session token may use —
so widening is the human's act, never the model's.

There is deliberately **no bypass mode**. An agent that runs with no gate at all is the one
configuration whose blast radius is unbounded, and sessions now create sessions without a human
in the loop, so the mode that disables the loop entirely is not offered. The loosest policy
available is :attr:`AUTO`, whose reviewer judges every gate it is handed and answers for
itself — but it does answer, and a request it cannot vouch for is refused rather than run.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Optional


class PermissionMode(StrEnum):
    """Who answers when a call asks to reach past its confinement. A :class:`~enum.StrEnum`, so
    it is equal to (and serializes as) its wire/config/database string and no boundary
    special-cases it.

    - ``ASK`` — the person running the session answers. The turn parks until they do.
    - ``AUTO`` — the reviewer answers: it allows the request or refuses it, and never asks.

    ``AUTO`` is for work nobody is watching. An agent sent off to do a job cannot be autonomous
    and also stop every few minutes for a click — a mode that escalates is a mode that needs
    somebody at the keyboard, which is the opposite of what it is for. So the reviewer is given
    the decision: it allows what it can vouch for and refuses the rest, and a refusal reaches
    the model as something it can work around rather than a prompt nobody is there to answer.
    The protection is that the reviewer still runs on every gate, and still fails closed.

    How much a session may do *without* asking anybody is not here. It is the confinement, and
    it is configured under ``sandbox`` where it belongs.
    """

    ASK = "ask"
    AUTO = "auto"

    @classmethod
    def parse(cls, value: str | PermissionMode | None) -> Optional[PermissionMode]:
        """The mode a string names, or ``None`` when it names no known mode — so a caller can
        tell 'absent or invalid' apart from a real choice.

        The two spellings above are the whole of it. A name this does not know is not a mode,
        and reading it as one would mean two spellings for a policy and configuration files that
        disagree about which is real."""
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError:
            return None

    @classmethod
    def coerce(cls, value: str | PermissionMode | None, default: PermissionMode | None = None) -> PermissionMode:
        """The mode a value names, falling back to ``default`` (or :attr:`ASK`) when it names
        none. The typed parse to apply at a string boundary."""
        parsed = cls.parse(value)
        return parsed if parsed is not None else (default if default is not None else cls.ASK)

    @property
    def restrictiveness(self) -> int:
        """Position in the restrictiveness order, least to most: ``auto < ask``.

        ``auto`` is the looser of the two because it is the one that can let a request through
        with nobody watching, and that — what a session may do without a person — is the axis
        the clamp cares about."""
        return 1 if self is PermissionMode.ASK else 0

    @classmethod
    def more_restrictive(cls, *modes: str | PermissionMode | None) -> PermissionMode:
        """The more restrictive of the given modes — a meet on the restrictiveness order.
        Unknown or absent inputs are ignored; with none given the interactive default applies.
        This is the child-session clamp: a created session runs at the more restrictive of its
        parent's mode and the mode its creator asked for, so a child can never be looser than
        the session that created it."""
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
           forever, and the parent waits on it. This does not fall out of the clamp and cannot —
           ``ask`` is the *stricter* mode, so the clamp would happily pick it — which is why it
           is a constraint rather than a bound, and why asking for it under an unattended parent
           raises instead of being quietly answered.
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

    @property
    def never_asks(self) -> bool:
        """Whether this mode can run with nobody watching."""
        return self is PermissionMode.AUTO

    @property
    def asks(self) -> bool:
        """Whether a gate goes to a person. The complement of :attr:`never_asks`, spelled out
        because the read sites are many and the negation reads badly at each of them."""
        return self is PermissionMode.ASK
