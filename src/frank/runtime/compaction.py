"""The AgentRuntime compaction concern (a mixin composed into AgentRuntime).

Observational Memory: deciding when to compact, running the Observer/Reflector model calls, and
rewriting the conversation into a dense observation log so an unbounded turn never overflows."""
from __future__ import annotations

import logging

from frank.base.tuning import count_tokens
from frank.runtime.internals import ObservationBatch, conversation_tokens, message_tokens
from frank.runtime.turn_events import CompactionDone, CompactionStarted, TurnEvent
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from pydantic import ValidationError
from typing import AsyncIterator
from frank.base.serialization import compact


logger = logging.getLogger(__name__)


class _CompactsContext:
    """Keeping a long conversation inside its context window.

    Observational memory — the Observer and Reflector — plus the token arithmetic that decides
    when either runs."""

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

    def _usable_context(self) -> int:
        """How much of the window a conversation may occupy before it must be folded.

        Not the whole window. A request that exactly fills it leaves the model nowhere to answer,
        and leaves compaction — itself a model call — nowhere to run. A window of zero means "not
        known yet" and answers zero rather than inventing a budget."""
        window = self._context_window
        if window <= 0:
            return 0
        return max(0, window - int(window * self._global_configuration.compaction.output_reserve_fraction))

    def _recent_working_set(self) -> int:
        """The tokens of recent conversation treated as still in use — the tail kept verbatim
        rather than folded into the log.

        A share of the usable window rather than a fixed count, so it scales with the model
        instead of being one figure chosen for one window size."""
        return int(self._usable_context() * self._global_configuration.compaction.recent_working_set_fraction)

    def _observer_boundary(self) -> int:
        """Index splitting the conversation into ``[older to fold] | [recent kept verbatim]``.

        Chosen by *size*, not by counting user turns, and that is the whole point. The tail used
        to be "the last N user messages", which silently assumed a conversation alternates. An
        agentic run does not: one instruction can be followed by two hundred tool results, and
        under a turn count that whole run reads as a single turn with nothing old enough to fold.
        A session doing exactly that grew to 286k tokens against a 272k window and never folded
        once, because the boundary was 0 every time it was asked.

        So the tail is a token budget, filled backwards from the end. When one turn alone exceeds
        the budget the cut lands *inside* it, which is what makes a long unattended run
        compactable at all.

        The cut is then advanced to a safe point: never onto a ToolMessage, because a tool result
        whose call has been folded away is an orphan the provider rejects. Everything before the
        cut goes to the Observer; the tail is sent verbatim. Returns 0 when nothing can be folded.
        """
        messages = self._conversation
        budget = self._recent_working_set()
        start = len(messages)
        carried = 0
        for index in range(len(messages) - 1, -1, -1):
            carried += message_tokens(messages[index])
            if carried > budget and index < len(messages) - 1:
                break
            start = index
        # A tool result must stay with the call it answers, so walk forward off any ToolMessage
        # the budget happened to land on. An AIMessage left holding calls whose results moved
        # into the tail would be the same break seen from the other side, so this skips the
        # whole group rather than just its first message.
        while start < len(messages) and isinstance(messages[start], ToolMessage):
            start += 1
        # Nothing to fold: either the whole conversation fits the tail, or the tail is everything
        # after a cut that could not be placed anywhere useful.
        return start if 0 < start < len(messages) else 0

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
        """Whether the live context has grown enough that folding is worth what it costs.

        Deliberately late. A fold is the only thing here that rewrites the conversation, and a
        rewritten prefix is a prompt cache thrown away — so every fold is paid for twice, once in
        the two model calls it takes and again in the full-price re-read on the call after. Held
        context, by contrast, is cheap while the cache holds: a provider bills a cached prefix at
        a fraction of a fresh one. So the conversation is left alone for as long as it safely can
        be, and the reserve below the window is what keeps "as long as it can be" from becoming
        one turn too long.

        Simulated across session lengths, folding at 0.85 of the usable window came to ~0.94x the
        tokens of folding at 0.6, and folding later still was *worse* — at 0.95 each fold has a
        larger head for the Observer to read, and that is paid every time. There is no version of
        this that waits for the window to be exceeded: by then the request has already failed."""
        usable = self._usable_context()
        return usable > 0 and self._latest_context_tokens >= (
            self._global_configuration.compaction.reclaim_at_fraction * usable
        )

    def _should_compact(self) -> bool:
        """Auto-compaction trigger.

        A supplied strategy answers this itself. The default is: enabled in configuration, live
        context past the observer fraction of the *usable* window, and something to fold. Measured
        against the usable window rather than the raw one so the fraction means what it says —
        against the raw number, "60% full" was already past the point where a fold had room to
        run. Manual compaction ignores this and always runs a pass."""
        if self._compaction is not None:
            return bool(self._compaction.should_compact(self._compaction_state()))
        compaction = self._global_configuration.compaction
        if not compaction.automatic or not self._at_folding_threshold():
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
        # The log is excluded from the tail as well as from the fold. It is rebuilt and placed at
        # the front below, so a boundary that happened to leave it on the recent side would put
        # two copies of the memory in the conversation rather than moving it.
        recent = [message for message in self._conversation[boundary:] if message is not observation_message]
        if not older:
            # The boundary landed just past the observation log with nothing but the log behind
            # it — which happens once a folded conversation's tail alone fills the budget. There
            # is nothing to fold, and asking the Observer to summarize an empty list would spend
            # a model call on every iteration to be told so.
            return
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
        merged_tokens = count_tokens(compact(merged))
        usable = self._usable_context()
        if usable > 0 and merged_tokens > compaction.condense_log_at_fraction * usable:
            merged = await self._reflect(merged)
        # Replace in place: the conversation list object is shared with the executor's
        # per-context store, so mutating the same object keeps that binding.
        self._conversation[:] = [self._build_observation_message(merged), *recent]
        # Occupancy no longer describes the conversation, so it is measured again rather than
        # zeroed. Zeroing was enough while the only question afterwards was "do not fold again
        # immediately", which any small number answers. It is not enough now that something runs
        # *after* a fold and has to know whether the fold was sufficient: a zero would report
        # every conversation as empty and the fallback could never fire. Counting the real thing
        # answers both, and answers the second one honestly.
        self._latest_context_tokens = conversation_tokens(self._conversation)
        yield CompactionDone(reason=reason, ok=True,
            observations_added=len(new_observations),
            messages_before=messages_before,
            messages_after=len(self._conversation),
            tokens_before=tokens_before,
            tokens_after=self._latest_context_tokens,
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
