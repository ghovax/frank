"""The runtime's compaction concern: when to fold a conversation, and the observation log it folds into."""
from __future__ import annotations

import logging

from frank.base.message_content import forget_carried_reasoning
from frank.base.tuning import count_tokens
from frank.runtime.internals import ObservationBatch, conversation_tokens, message_tokens
from frank.runtime.turn_events import CompactionDone, CompactionStarted, TurnEvent
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from pydantic import ValidationError
from typing import AsyncIterator
from frank.base.serialization import compact


logger = logging.getLogger(__name__)


def _without_provider_reasoning(messages: list) -> list:
    """The same messages with the provider-native reasoning cut out, since the turns it explained are gone."""
    for message in messages:
        forget_carried_reasoning(message)
    return messages


class _CompactsContext:
    """Keeping a long conversation inside its window: the Observer and Reflector, and the arithmetic that triggers them."""

    def _observation_message(self):
        """The observation log, found by its marker rather than by its type, because the marker survives persistence."""
        for message in self._conversation:
            if message.additional_kwargs.get("observation_log"):
                return message
        return None

    async def _emit_observations(self, request: list) -> list[dict]:
        """Run one Observer or Reflector call and read its structured output from the tool call it was forced to make."""
        model = self._llm.bind_tools([ObservationBatch], tool_choice="required")
        response = await model.ainvoke(request)
        if response is None or not response.tool_calls:
            return []
        try:
            batch = ObservationBatch.model_validate(response.tool_calls[0]["args"])
        except ValidationError:
            return []
        return [observation.model_dump() for observation in batch.observations]

    def _observations_of(self, message: SystemMessage | None) -> list[dict]:
        raw = message.additional_kwargs.get("observations") if message else None
        return list(raw) if isinstance(raw, list) else []

    def _build_observation_message(self, observations: list[dict]):
        """The observation log the agent passively reads, with the entries riding in metadata so a later pass appends to data."""
        return self._reminder_message(
            self._prompt_loader.load("observation_log", {"observations": compact(observations)}),
            marks={"observation_log": True, "observations": observations},
        )

    def _usable_context(self) -> int:
        """How much of the window a conversation may occupy, leaving room for the answer and for the fold itself."""
        window = self._context_window
        if window <= 0:
            return 0
        return max(0, window - int(window * self._global_configuration.compaction.output_reserve_fraction))

    def _recent_working_set(self) -> int:
        """The tail kept verbatim rather than folded, as a share of the usable window so it scales with the model."""
        return int(self._usable_context() * self._global_configuration.compaction.recent_working_set_fraction)

    def _observer_boundary(self) -> int:
        """Index splitting the conversation into what is folded and what is kept, chosen by size rather than by turn count."""
        messages = self._conversation
        budget = self._recent_working_set()
        start = len(messages)
        carried = 0
        for index in range(len(messages) - 1, -1, -1):
            carried += message_tokens(messages[index])
            if carried > budget and index < len(messages) - 1:
                break
            start = index
        # A tool result must stay with the call it answers, so walk forward off any tool message the budget landed on.
        while start < len(messages) and isinstance(messages[start], ToolMessage):
            start += 1
        # Nothing to fold: either the whole conversation fits the tail, or the cut could not be placed usefully.
        return start if 0 < start < len(messages) else 0

    def _carried_user_messages(self, folded: list) -> list:
        """The user's own messages from the folded turns, kept word for word, because a summariser is worst on exactly these."""
        spoken = [
            message for message in folded
            if isinstance(message, HumanMessage) and not message.additional_kwargs.get("reminder")
        ]
        if not spoken:
            return []
        budget = int(self._usable_context() * self._global_configuration.compaction.verbatim_user_fraction)
        first, rest = spoken[0], spoken[1:]
        carried = [first]
        spent = message_tokens(first)
        for message in reversed(rest):
            size = message_tokens(message)
            if spent + size > budget:
                break
            carried.append(message)
            spent += size
        # Back into the order they were said in, since the rest were collected newest-first.
        carried[1:] = list(reversed(carried[1:]))
        carried.reverse()
        return carried

    def _compaction_state(self, reason: str = "auto"):
        """What a supplied strategy is given. Passed, never reached for."""
        from frank.base.ports import CompactionState

        return CompactionState(
            messages=list(self._conversation),
            context_window=self._context_window,
            context_tokens=self._latest_context_tokens,
            reason=reason,
        )

    def _at_folding_threshold(self) -> bool:
        """Whether the live context has grown enough that folding is worth the prompt cache it throws away."""
        usable = self._usable_context()
        return usable > 0 and self._latest_context_tokens >= (
            self._global_configuration.compaction.reclaim_at_fraction * usable
        )

    def _should_compact(self) -> bool:
        """The automatic trigger, measured against the usable window, unless a supplied strategy answers it instead."""
        if self._compaction is not None:
            return bool(self._compaction.should_compact(self._compaction_state()))
        compaction = self._global_configuration.compaction
        if not compaction.automatic or not self._at_folding_threshold():
            return False
        return self._observer_boundary() > 0

    async def _fold_into_observations(self, older: list, existing: list[dict]) -> list[dict]:
        """Fold the older messages into new structured observations to append. The
        messages are handed to the model as-is; the existing memory is shown so it does
        not duplicate what is already recorded.

        Named for what it does rather than for the Observer that does it. As `_observe` it was
        shadowed by `AgentRuntime._observe`, the audit sink — a mixin loses to a method the class
        defines itself — so a fold called the audit trail with `(older, existing)` as its
        `(kind, data)` and then awaited its `None`. Nothing caught it because nothing reached it:
        the boundary this used to depend on answered 0 for every conversation, so the fold
        returned before ever calling this.""",
        existing_json = compact(existing) if existing else "[]"
        instructions = self._prompt_loader.load("observer", {"existing_observations": existing_json})
        return await self._emit_observations([
            SystemMessage(content=instructions),
            *older,
            HumanMessage(content="Record the observations now."),
        ])

    async def _reflect(self, observations: list[dict]) -> list[dict]:
        """Merge and condense the structured memory, keeping the originals if reflection returns nothing."""
        goals = [entry for entry in observations if entry.get("category") == "goal"]
        rest = [entry for entry in observations if entry.get("category") != "goal"]
        if not rest:
            return observations
        reflected = await self._emit_observations([
            SystemMessage(content=self._prompt_loader.load("reflector", {"observations": compact(rest)})),
            HumanMessage(content="Record the condensed memory now."),
        ])
        return [*goals, *reflected] if reflected else observations

    async def compact(self, reason: str = "manual") -> AsyncIterator[TurnEvent]:
        """Observational-memory compaction: the Observer folds the older turns into the log and the Reflector condenses it."""
        # A supplied strategy replaces the Observer and Reflector pass entirely, while the events around it stay the runtime's.
        if self._compaction is not None:
            state = self._compaction_state(reason)
            messages_before = len(self._conversation)
            tokens_before = self._latest_context_tokens
            yield CompactionStarted(
                reason=reason, messages_before=messages_before, tokens_before=tokens_before,
            )
            self._conversation[:] = _without_provider_reasoning(await self._compaction.compact(state))
            yield CompactionDone(
                reason=reason, ok=True,
                messages_before=messages_before, messages_after=len(self._conversation),
                tokens_before=tokens_before, tokens_after=self._latest_context_tokens,
            )
            return
        boundary = self._observer_boundary()
        if boundary <= 0:
            return
        observation_message = self._observation_message()
        existing = self._observations_of(observation_message)
        # Fold every older message except the existing observation block (it is rebuilt).
        older = [message for message in self._conversation[:boundary] if message is not observation_message]
        # The log is excluded from the tail as well as the fold, since it is rebuilt and placed at the front below.
        recent = [message for message in self._conversation[boundary:] if message is not observation_message]
        if not older:
            # The boundary landed just past the log with nothing behind it, so there is nothing to fold.
            return
        tokens_before = self._latest_context_tokens
        messages_before = len(self._conversation)
        yield CompactionStarted(reason=reason,
            messages_before=messages_before,
            tokens_before=tokens_before,
        )
        new_observations = await self._fold_into_observations(older, existing)
        if not new_observations:
            # Produced nothing parseable — leave history untouched rather than drop it.
            yield CompactionDone(reason=reason, ok=False,
                messages_before=messages_before,
                messages_after=messages_before,
                tokens_before=tokens_before,
            )
            return
        merged = [*existing, *new_observations]
        compaction = self._global_configuration.compaction
        merged_tokens = count_tokens(compact(merged))
        usable = self._usable_context()
        if usable > 0 and merged_tokens > compaction.condense_log_at_fraction * usable:
            merged = await self._reflect(merged)
        # Replace in place, because the conversation list object is shared with the executor's per-context store.
        self._conversation[:] = [
            *self._carried_user_messages(older),
            self._build_observation_message(merged),
            *_without_provider_reasoning(recent),
        ]
        # Occupancy no longer describes the conversation, so it is measured again rather than zeroed.
        self._latest_context_tokens = conversation_tokens(self._conversation)
        yield CompactionDone(reason=reason, ok=True,
            observations_added=len(new_observations),
            messages_before=messages_before,
            messages_after=len(self._conversation),
            tokens_before=tokens_before,
            tokens_after=self._latest_context_tokens,
            log_tokens=count_tokens(compact(merged)),
        )


class KeepRecentTurns:
    """Keep the last `keep` exchanges and drop the rest, with no model call and no cost."""

    def __init__(self, keep: int = 20) -> None:
        if keep < 1:
            raise ValueError(f"keep must be at least 1, got {keep}.")
        self._keep = keep

    def should_compact(self, state) -> bool:
        return len(state.messages) > self._keep * 2

    async def compact(self, state) -> list:
        return list(state.messages[-self._keep * 2:])


__all__ = ["KeepRecentTurns"]
