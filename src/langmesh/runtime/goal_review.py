"""The goal review: a pass of its own that reads the session and decides where its goal stands."""

from __future__ import annotations

import logging
from typing import Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, model_validator

from langmesh.base.serialization import lines
from langmesh.base.tuning import Tunable, active_tuning
from langmesh.runtime.goal import Goal
from langmesh.runtime.internals import emit_structured, says


logger = logging.getLogger(__name__)

#: Where a goal stands after one reading of the work, which is not the same as what the session says about it.
GOAL_STANDING = Literal["unmet", "satisfied", "blocked"]


class GoalReview(BaseModel):
    """One reading of an open goal: where it stands, and what the session is told to do about it."""

    # The order is the order it is written in: the evidence first, the verdict after, so the verdict follows it.
    assessment: str = Field(description=says("goal_review_assessment"))
    unmet: list[str] = Field(default_factory=list, description=says("goal_review_unmet"))
    standing: GOAL_STANDING = Field(description=says("goal_review_standing"))
    direction: str = Field(default="", description=says("goal_review_direction"))
    evidence: str = Field(default="", description=says("goal_review_evidence"))
    blocker: str = Field(default="", description=says("goal_review_blocker"))

    @model_validator(mode="after")
    def _carry_what_the_verdict_rests_on(self):
        """A verdict without what establishes it is not a verdict, so the pass is retried rather than believed."""
        if self.standing == "satisfied":
            if self.unmet:
                raise ValueError("A satisfied goal has nothing unmet.")
            if not self.evidence.strip():
                raise ValueError(
                    "A satisfied goal needs the evidence that proves each requirement."
                )
            return self
        if not self.unmet:
            raise ValueError(
                "A goal that is not satisfied has something unmet: name the requirements it is."
            )
        if not self.direction.strip():
            raise ValueError("An unfinished goal needs a direction: what the session does next.")
        if self.standing == "blocked" and not self.blocker.strip():
            raise ValueError("A blocked goal needs what is in the way and what would clear it.")
        return self


class _ReviewsGoal:
    """Deciding where an open goal stands, in a pass of its own so a session never grades its own work."""

    async def review_goal(self) -> Optional[GoalReview]:
        """Read this session against its goal and answer with the verdict, or ``None`` if the pass never landed."""
        goal = self.goal
        if goal is None or not goal.is_open:
            return None
        instructions = self._prompt_loader.load(
            "goal_review",
            {
                "goal": goal.text,
                # Prefixed here rather than in the template, so an unstated purpose leaves no orphaned label.
                "purpose": f"What that is for: {goal.purpose}" if goal.purpose else "",
                "requirements": lines(goal.requirements),
                # What it last told the session, so it can see whether the session did it before saying it again.
                "previous_direction": goal.direction,
                # An impasse claimed sooner than this is one more push, and the reviewer is told so plainly.
                "blocked_turns": active_tuning().amount(Tunable.goal_blocked_turns),
            },
        )
        return await emit_structured(
            self._llm,
            GoalReview,
            [
                SystemMessage(content=instructions),
                # The session as it stands, which is the whole of what the verdict is allowed to rest on.
                *self._conversation,
                HumanMessage(content=self._prompt_loader.load("goal_review_now", {})),
            ],
            "goal review",
            active_tuning().amount(Tunable.goal_review_attempts),
        )

    def apply_goal_review(self, review: Optional[GoalReview]) -> Optional[Goal]:
        """Write the verdict onto the goal and answer with it, so the caller reads one value rather than two."""
        goal = self.goal
        if goal is None or not goal.is_open:
            return goal
        if review is None:
            # The pass could not be reached. A goal is not resolved by a failure to look at it, so the work goes on.
            logger.warning("the goal review did not land; carrying the goal on unchanged")
            return goal
        if review.standing == "satisfied":
            self.write_goal(
                goal.model_copy(update={"status": Goal.SATISFIED, "evidence": review.evidence})
            )
            return self.goal
        # An impasse is only accepted once the goal has actually been pushed, so one bad turn cannot end it.
        settled = review.standing == "blocked" and goal.continuations >= active_tuning().amount(
            Tunable.goal_blocked_turns
        )
        if settled:
            self.write_goal(
                goal.model_copy(
                    update={
                        "status": Goal.BLOCKED,
                        "blocker": review.blocker,
                        "direction": review.direction,
                    }
                )
            )
            return self.goal
        if review.standing == "blocked":
            logger.info(
                "an impasse was reported before the goal had been pushed; carrying on instead",
                extra={"continuations": goal.continuations},
            )
        self.write_goal(goal.model_copy(update={"direction": review.direction}))
        return self.goal
