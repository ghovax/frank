"""The AgentRuntime compaction concern (a mixin composed into AgentRuntime).

Observational Memory: deciding when to compact, running the Observer/Reflector model calls, and
rewriting the conversation into a dense observation log so an unbounded turn never overflows."""
from __future__ import annotations

from frank.runtime.internals import ObservationBatch
from frank.runtime.turn_events import CompactionDone, CompactionStarted, TurnEvent
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError
from typing import AsyncIterator
from frank.base.serialization import compact


class _CompactsContext:
    """Keeping a long conversation inside its context window.

    Observational memory — the Observer and Reflector — plus the token arithmetic that decides
    when either runs."""

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Fast local token estimate (~4 chars/token) for sizing the observation log,
        which has no provider usage figure of its own."""
        return len(text) // 4

    def _observation_message(self) -> SystemMessage | None:
        """The observation-log system message (the folded conversation memory), if one
        exists. Tagged in ``additional_kwargs`` so it survives persistence + reload and
        can be found and appended to."""
        for message in self._conversation:
            if isinstance(message, SystemMessage) and message.additional_kwargs.get("observation_log"):
                return message
        return None

    async def _emit_observations(self, request: list) -> list[dict]:
        """Run one Observer/Reflector call and read its structured output from the
        model's ``ObservationBatch`` tool call — the shape is guaranteed by tool-calling,
        not scraped from free text (same pattern as PermissionDecision). Returns []
        when the model emits no tool call, so a miss simply changes nothing."""
        model = self._llm.bind_tools([ObservationBatch], tool_choice="auto")
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

    def _build_observation_message(self, observations: list[dict]) -> SystemMessage:
        """The observation log the main agent passively reads: the structured entries
        dumped as JSON into the render template. The list itself rides in
        ``additional_kwargs`` so a later pass appends to structured data, not to prose."""
        rendered = compact(observations)
        return SystemMessage(
            content=self._prompt_loader.load("observation_log", {"observations": rendered}),
            additional_kwargs={"observation_log": True, "observations": observations},
        )

    def _observer_boundary(self) -> int:
        """Index splitting the conversation into ``[older to fold] | [recent kept
        verbatim]``. Cuts at a user-turn boundary (a HumanMessage) so tool_call/
        tool_result pairing stays intact and the kept tail is a whole number of recent
        turns. Returns 0 when nothing is old enough to fold."""
        keep = max(1, self._global_configuration.compaction.keep_recent_turns)
        human_indices = [
            index for index, message in enumerate(self._conversation)
            if isinstance(message, HumanMessage)
            # Harness notes ride in user-role messages for cache reasons but are
            # not user turns — they must not shift the keep-recent boundary.
            and not message.additional_kwargs.get("harness_note")
        ]
        if len(human_indices) <= keep:
            return 0
        return human_indices[-keep]

    def _compaction_state(self, reason: str = "auto"):
        """What a supplied strategy is given. Passed, never reached for."""
        from frank.base.ports import CompactionState

        return CompactionState(
            messages=list(self._conversation),
            context_window=self._context_window,
            context_tokens=self._latest_context_tokens,
            reason=reason,
        )

    def _should_compact(self) -> bool:
        """Auto-compaction trigger.

        A supplied strategy answers this itself. The default is: enabled in configuration,
        live context past the observer fraction of the window, and something old enough to
        fold. Manual compaction ignores this and always runs a pass."""
        if self._compaction is not None:
            return bool(self._compaction.should_compact(self._compaction_state()))
        compaction = self._global_configuration.compaction
        if not compaction.auto or self._context_window <= 0:
            return False
        if self._latest_context_tokens < compaction.observer_context_fraction * self._context_window:
            return False
        return self._observer_boundary() > 0

    async def _observe(self, older: list, existing: list[dict]) -> list[dict]:
        """Fold the older messages into new structured observations to append. The
        messages are handed to the model as-is; the existing memory is shown so it does
        not duplicate what is already recorded."""
        existing_json = compact(existing) if existing else "[]"
        instructions = self._prompt_loader.load("observer", {"existing_observations": existing_json})
        return await self._emit_observations([
            SystemMessage(content=instructions),
            *older,
            HumanMessage(content="Record the observations now."),
        ])

    async def _reflect(self, observations: list[dict]) -> list[dict]:
        """Merge and condense the structured memory. Keeps the original entries if
        reflection returns nothing, so it never loses memory."""
        instructions = self._prompt_loader.load(
            "reflector", {"observations": compact(observations)}
        )
        reflected = await self._emit_observations([
            SystemMessage(content=instructions),
            HumanMessage(content="Record the condensed memory now."),
        ])
        return reflected or observations

    async def compact(self, reason: str = "manual") -> AsyncIterator[TurnEvent]:
        """Observational-memory compaction (replaces wholesale summarization). The
        Observer folds the older turns into the observation log — appended to what is
        already there, so the observation prefix stays cache-stable — and drops their raw
        messages; the Reflector condenses the log when it grows past its fraction of the
        window. Recent turns stay verbatim. Yields COMPACTION_STARTED/DONE for the UI. A
        no-op yields nothing. Manual calls force a pass; the auto trigger is gated by
        :meth:`_should_compact`."""
        # A supplied strategy replaces the Observer/Reflector pass entirely. It answers with
        # the conversation to carry forward and calls no model unless it wants to; the events
        # around it stay the runtime's, so the interface shows a compaction the same way
        # whichever strategy produced it.
        if self._compaction is not None:
            state = self._compaction_state(reason)
            messages_before = len(self._conversation)
            tokens_before = self._latest_context_tokens
            yield CompactionStarted(
                reason=reason, messages_before=messages_before, tokens_before=tokens_before,
            )
            self._conversation[:] = await self._compaction.compact(state)
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
        recent = list(self._conversation[boundary:])
        tokens_before = self._latest_context_tokens
        messages_before = len(self._conversation)
        yield CompactionStarted(reason=reason,
            messages_before=messages_before,
            tokens_before=tokens_before,
        )
        new_observations = await self._observe(older, existing)
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
        merged_tokens = self._estimate_tokens(compact(merged))
        if self._context_window > 0 and merged_tokens > compaction.reflector_observation_fraction * self._context_window:
            merged = await self._reflect(merged)
        # Replace in place: the conversation list object is shared with the executor's
        # per-context store, so mutating the same object keeps that binding.
        self._conversation[:] = [self._build_observation_message(merged), *recent]
        # Occupancy no longer reflects the (smaller) context; reset so auto-compaction
        # does not immediately re-fire before the next real model call.
        self._latest_context_tokens = 0
        yield CompactionDone(reason=reason, ok=True,
            observations_added=len(new_observations),
            messages_before=messages_before,
            messages_after=len(self._conversation),
            tokens_before=tokens_before,
        )


class KeepRecentTurns:
    """Keep the last `keep` exchanges and drop the rest. No model call, no cost.

    The default folds older turns into an observation log, which preserves long-horizon
    memory and costs two model calls each time it runs. That is right for a conversation
    someone returns to over days. It is wrong for a scripted agent with a budget and no
    memory to preserve, which wants the cheap deterministic answer — and until this existed,
    that program had no way to say so.

    Counts messages rather than turns, at two messages to a turn, because a conversation is a
    flat list here and a turn boundary is not marked in it.
    """

    def __init__(self, keep: int = 20) -> None:
        if keep < 1:
            raise ValueError(f"keep must be at least 1, got {keep}.")
        self._keep = keep

    def should_compact(self, state) -> bool:
        return len(state.messages) > self._keep * 2

    async def compact(self, state) -> list:
        return list(state.messages[-self._keep * 2:])


__all__ = ["KeepRecentTurns"]
