"""The session's goal: one contract for completion, durable across turns.

A leaf module, imported by the runtime that owns the goal, the tool concern that writes it, and
the worker that decides whether the session keeps working — so all three name the same type
rather than passing a dictionary between them and each guessing at its shape.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel


class Goal(BaseModel):
    """The session's single contract for completion, and the one piece of durable state that
    outlives the turn that set it.

    It is written by the agent (``update_goal``) and read by two other parties: the model, which
    is shown it whenever its situation changes, and the person, who sees it in the interface with
    a control to call it off. What makes it more than a note to self is that a session with an
    open goal does not go quiet when a turn ends — another turn is opened for it — so the goal,
    rather than the end of a turn, is what decides when the work stops.

    ``requirements`` is why this is a record and not a sentence. An outcome nobody wrote down as
    checkable conditions cannot be audited at the end, and the audit is the whole point: without
    it, "done" means the model's memory of having worked, which is exactly the failure a goal
    exists to prevent.
    """

    #: The end state, in the agent's own words.
    text: str
    #: The conditions that must hold for the goal to be met, each one checkable.
    requirements: list[str] = []
    status: str = "active"
    #: What is in the way, set when the agent reports the goal blocked.
    blocker: str = ""
    #: How many turns have been opened for this goal since a person last spoke. Bookkeeping for
    #: the allowance, and deliberately absent from :meth:`for_model`: what keeps the session
    #: working is not the model's business, and one told it has "three passes left" starts
    #: rationing its work against a number it should never have seen.
    continuations: int = 0

    #: Being worked, so the session keeps going on its own.
    ACTIVE: ClassVar[str] = "active"
    #: The agent reported an impasse it cannot pass without the person. Nothing further is opened.
    BLOCKED: ClassVar[str] = "blocked"
    #: Set when the goal used its whole allowance without a person saying anything. Distinct from
    #: ``blocked``: nobody claimed the work is stuck, only that it has run long enough unattended
    #: to be worth a look.
    PARKED: ClassVar[str] = "parked"

    @property
    def is_open(self) -> bool:
        """Whether this goal is still being worked, as opposed to waiting on a person."""
        return self.status == self.ACTIVE

    def for_model(self) -> dict:
        """What the agent is shown: the goal itself, never the bookkeeping around it."""
        picture: dict[str, Any] = {"goal": self.text}
        if self.requirements:
            picture["requirements"] = list(self.requirements)
        if self.status != self.ACTIVE:
            picture["status"] = self.status
        if self.blocker:
            picture["blocker"] = self.blocker
        return picture

    def public(self) -> dict:
        """What the interface shows: the goal, its requirements, and where it stands."""
        return {
            "text": self.text,
            "requirements": list(self.requirements),
            "status": self.status,
            "blocker": self.blocker,
        }
