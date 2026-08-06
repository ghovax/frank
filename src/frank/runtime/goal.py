"""The session's goal: one contract for completion, durable across turns."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel


class Goal(BaseModel):
    """The session's single contract for completion, and the one piece of durable state that outlives the turn that set it."""

    #: The end state, in the agent's own words.
    text: str
    #: The conditions that must hold for the goal to be met, each one checkable.
    requirements: list[str] = []
    status: str = "active"
    #: What is in the way, set when the agent reports the goal blocked.
    blocker: str = ""
    #: How many turns have been opened for this goal since a person last spoke.
    continuations: int = 0

    #: Being worked, so the session keeps going on its own.
    ACTIVE: ClassVar[str] = "active"
    #: The agent reported an impasse it cannot pass without the person. Nothing further is opened.
    BLOCKED: ClassVar[str] = "blocked"
    #: Set when the goal used its whole allowance without a person saying anything.
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
