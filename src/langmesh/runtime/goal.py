"""The session's goal: one contract for completion, durable across turns."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel


class Goal(BaseModel):
    """The session's single contract for completion. The agent states it; the review decides where it stands."""

    #: The end state, in the agent's own words.
    text: str
    #: What that end state is for, which is what lets a closed route be told apart from a lost goal.
    purpose: str = ""
    #: The conditions that must hold for the goal to be met, each one checkable.
    requirements: list[str] = []
    status: str = "active"
    #: What is in the way, written by the review when it accepts that nothing here can pass it.
    blocker: str = ""
    #: What proved each requirement, written by the review when it accepts the goal as met.
    evidence: str = ""
    #: The review's instruction to the session, which opens the turn that follows it.
    direction: str = ""
    #: How many turns have been opened since a person last spoke, and deliberately not shown to the model.
    continuations: int = 0

    #: Being worked, so the session keeps going on its own.
    ACTIVE: ClassVar[str] = "active"
    #: The review found an impasse the session cannot pass without the person. Nothing further is opened.
    BLOCKED: ClassVar[str] = "blocked"
    #: Set when the goal used its whole allowance, which is distinct from anyone judging it stuck.
    PARKED: ClassVar[str] = "parked"
    #: Reached, and kept rather than dropped so the person can see what was reached and take it up again.
    SATISFIED: ClassVar[str] = "satisfied"
    #: No longer what the person wants, kept for the same reason. Only a person sets this.
    CLEARED: ClassVar[str] = "cleared"

    @property
    def is_open(self) -> bool:
        """Whether this goal is still being worked, as opposed to waiting on a person."""
        return self.status == self.ACTIVE

    def for_model(self) -> dict:
        """What the agent is shown: the goal itself, never the bookkeeping around it."""
        picture: dict[str, Any] = {"goal": self.text}
        if self.purpose:
            picture["purpose"] = self.purpose
        if self.requirements:
            picture["requirements"] = list(self.requirements)
        if self.status != self.ACTIVE:
            picture["status"] = self.status
        if self.blocker:
            picture["blocker"] = self.blocker
        return picture

    def public(self) -> dict:
        """What the interface shows: the goal, what it is for, its requirements, and where it stands."""
        return {
            "text": self.text,
            "purpose": self.purpose,
            "requirements": list(self.requirements),
            "status": self.status,
            "blocker": self.blocker,
            "evidence": self.evidence,
            "direction": self.direction,
        }
