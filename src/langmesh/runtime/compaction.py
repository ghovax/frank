"""The runtime's compaction concern: when to fold a conversation, and the observation log it folds into."""
from __future__ import annotations

import asyncio
import logging
from itertools import accumulate, takewhile

from langmesh.base.message_content import forget_carried_reasoning
from langmesh.base.tuning import Tunable, active_tuning, count_tokens
from langmesh.runtime.internals import DirectiveBatch, ObservationBatch, conversation_tokens, message_tokens
from langmesh.runtime.turn_events import CompactionDone, CompactionStarted, TurnEvent
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from pydantic import ValidationError
from typing import AsyncIterator
from langmesh.base.errors import log_fields
from langmesh.base.serialization import lines


logger = logging.getLogger(__name__)


def _without_provider_reasoning(messages: list) -> list:
    """The same messages with the provider-native reasoning cut out, since the turns it explained are gone."""
    # A sweep for its effect, not a transformation: `forget_carried_reasoning` edits each message in place.
    for message in messages:
        forget_carried_reasoning(message)
    return messages


class _CompactsContext:
    """Keeping a long conversation inside its window: the Observer and Reflector, and the arithmetic that triggers them."""

    def _observation_message(self):
        """The observation log, found by its marker rather than by its type, because the marker survives persistence."""
        return next(
            (message for message in self._conversation if message.additional_kwargs.get("observation_log")),
            None,
        )

    async def _emit_batch(self, schema, request: list, what: str):
        """One structured call: offered rather than forced, insisted on by the prompt, retried, and validated."""
        # The tool is offered and the prompt insists on it: forcing it, a thinking model behind a gateway refuses.
        model = self._llm.bind_tools([schema], tool_choice="auto")
        attempts = active_tuning().amount(Tunable.compaction_attempts)
        for attempt in range(1, attempts + 1):
            try:
                response = await model.ainvoke(request)
            except Exception:  # noqa: BLE001 — one dropped call is not the end of the fold
                logger.warning("the %s pass could not be reached (attempt %d of %d)", what, attempt, attempts, exc_info=True)
                continue
            if response is None or not response.tool_calls:
                logger.warning("the %s pass answered without recording anything (attempt %d of %d)", what, attempt, attempts)
                continue
            try:
                return schema.model_validate(response.tool_calls[0]["args"])
            except ValidationError:
                logger.warning("the %s pass did not fit its schema (attempt %d of %d)", what, attempt, attempts, exc_info=True)
                continue
        return None

    async def _emit_observations(self, request: list) -> list:
        """Run one Observer or Reflector call and read its entries from the tool call it makes."""
        batch = await self._emit_batch(ObservationBatch, request, "observer")
        return list(batch.observations) if batch else []

    def _observations_of(self, message: SystemMessage | None) -> list[dict]:
        raw = message.additional_kwargs.get("observations") if message else None
        return list(raw) if isinstance(raw, list) else []

    def _directives_of(self, message: SystemMessage | None) -> list[dict]:
        raw = message.additional_kwargs.get("directives") if message else None
        return list(raw) if isinstance(raw, list) else []

    @staticmethod
    def _live(entries: list[dict]) -> list[dict]:
        """What nothing later replaced. The superseded stay stored; they simply stop being read."""
        replaced = {str(one) for entry in entries for one in (entry.get("supersedes") or [])}
        return [entry for entry in entries if str(entry.get("id") or "") not in replaced]

    @staticmethod
    def _identified(entries: list) -> list[dict]:
        """Each entry with its content address attached, which is how a later pass names it to revise it."""
        return [{**entry.model_dump(), "id": entry.identity()} for entry in entries]

    @staticmethod
    def _claims(entries: list[dict]) -> list[dict]:
        """The record as a later pass is shown it: enough to judge an entry and to name it, not its whole text."""
        fields = ("kind", "summary") if entries and "summary" in entries[0] else ("category", "claim")
        return [{"id": entry.get("id"), **{name: entry.get(name) for name in fields}} for entry in entries]

    def _build_observation_message(self, observations: list[dict], directives: list[dict]):
        """The record the agent passively reads, with both ledgers riding in metadata so a later pass appends to data."""
        return self._reminder_message(
            self._prompt_loader.load("observation_log", {
                "observations": lines(self._live(observations)),
                "directives": lines([one for one in self._live(directives) if one.get("still_binding", True)]),
            }),
            marks={"observation_log": True, "observations": observations, "directives": directives},
        )

    def _usable_context(self) -> int:
        """How much of the window a conversation may occupy, leaving room for the answer and for the fold itself."""
        window = self._context_window
        if window <= 0:
            return 0
        return max(0, window - int(window * self._global_configuration.compaction.output_reserve_fraction))

    def _recent_working_set(self, reason: str = "automatic") -> int:
        """The tail kept verbatim rather than folded, as a share of the usable window so it scales with the model."""
        fraction = self._global_configuration.compaction.recent_working_set_fraction
        budget = int(self._usable_context() * fraction)
        if reason != "manual":
            return budget
        # Asked for deliberately, so the tail is a share of what is actually there: a conversation far
        # short of the window still has something to fold, and the request does not quietly do nothing.
        # The record itself is not foldable, so counting it would inflate the share and fold nothing at all.
        log = self._observation_message()
        foldable = [message for message in self._conversation if message is not log]
        return min(budget, int(conversation_tokens(foldable) * fraction))

    def _observer_boundary(self, reason: str = "automatic") -> int:
        """Index splitting the conversation into what is folded and what is kept, chosen by size rather than by turn count."""
        messages = self._conversation
        budget = self._recent_working_set(reason)
        # The same reckoning the tail uses: running totals from the newest, taken while they stay inside the budget.
        # At least the newest turn is always kept, so a single turn larger than the budget still leaves a cut to make.
        running = accumulate(message_tokens(message) for message in reversed(messages))
        fitting = max(1, sum(1 for _ in takewhile(lambda total: total <= budget, running)))
        # A tool result must stay with the call it answers, so walk forward off any tool message the budget landed on.
        start = next(
            (index for index in range(max(len(messages) - fitting, 0), len(messages))
             if not isinstance(messages[index], ToolMessage)),
            len(messages),
        )
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
        spent = message_tokens(first)
        running = accumulate(message_tokens(message) for message in reversed(rest))
        fitting = sum(1 for _ in takewhile(lambda total: spent + total <= budget, running))
        # The first is always kept; the rest are the newest that fit, left in the order they were said.
        return [first, *rest[len(rest) - fitting:]] if fitting else [first]

    def _compaction_state(self, reason: str = "auto"):
        """What a supplied strategy is given. Passed, never reached for."""
        from langmesh.base.ports import CompactionState

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

    def _bounded_tail(self, recent: list) -> list:
        """The newest turns that fit the budget, taken whole, newest first until the next one would not fit.

        Recency is not smallness: one tool result can be larger than everything folded away, so a tail kept
        because it is recent is what saturated the window. A turn is kept or it is not; none is cut in half.
        What is left out is not lost — the record holds what mattered in it.
        """
        budget = self._recent_working_set()
        if budget <= 0 or not recent:
            return recent
        # How many of the newest turns fit: running totals from the end, taken while they stay inside the budget.
        running = accumulate(message_tokens(message) for message in reversed(recent))
        fitting = sum(1 for _ in takewhile(lambda total: total <= budget, running))
        # A tool result without the call it answers is not a conversation, so the tail never begins on one.
        keep_from = next(
            (index for index in range(len(recent) - fitting, len(recent))
             if not isinstance(recent[index], ToolMessage)),
            len(recent),
        )
        if keep_from:
            logger.info(
                "recent turns bounded to their budget",
                extra=log_fields(dropped=keep_from, kept=len(recent) - keep_from, budget=budget),
            )
        return recent[keep_from:]

    async def _append_to_ledgers(self, observations: list[dict], directives: list[dict]) -> None:
        """Write what a pass produced. The store is append-only, so this never revises and never deletes."""
        store = getattr(self, "_turn_store", None)
        session = getattr(self, "_session_id", "")
        if store is None or not session:
            return
        for ledger, entries in (("observations", observations), ("directives", directives)):
            if not entries:
                continue
            try:
                await store.append_ledger(session, ledger, entries)
            except Exception:  # noqa: BLE001 — the fold has already happened; losing the durable copy must not undo it
                logger.warning("could not append to the %s ledger", ledger, exc_info=True)

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
        shown = lines(self._claims(self._live(existing)))
        instructions = self._prompt_loader.load("observer", {"existing_observations": shown})
        entries = await self._emit_observations([
            SystemMessage(content=instructions),
            *older,
            HumanMessage(content=self._prompt_loader.load("observe_now", {})),
        ])
        return self._identified(entries)

    async def _fold_into_directives(self, older: list, existing: list[dict]) -> list[dict]:
        """What the person asked for in these turns, in their meaning rather than their words."""
        spoken = self._carried_user_messages(older)
        if not spoken:
            return []
        shown = lines(self._claims(self._live(existing)))
        instructions = self._prompt_loader.load("directives", {"existing_directives": shown})
        entries = await self._emit_batch(
            DirectiveBatch,
            [SystemMessage(content=instructions), *spoken, HumanMessage(content=self._prompt_loader.load("directives_now", {}))],
            "directive",
        )
        return self._identified(getattr(entries, "directives", []) if entries else [])

    async def _reflect(self, observations: list[dict]) -> list[dict]:
        """Merge and condense the structured memory, keeping the originals if reflection returns nothing."""
        goals = [entry for entry in observations if entry.get("category") == "goal"]
        rest = [entry for entry in observations if entry.get("category") != "goal"]
        if not rest:
            return observations
        reflected = await self._emit_observations([
            SystemMessage(content=self._prompt_loader.load("reflector", {"observations": lines(rest)})),
            HumanMessage(content=self._prompt_loader.load("reflect_now", {})),
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
        boundary = self._observer_boundary(reason)
        if boundary <= 0:
            # Said rather than done in silence, so a deliberate request is answered either way.
            held = len(self._conversation)
            tokens = self._latest_context_tokens
            yield CompactionStarted(reason=reason, messages_before=held, tokens_before=tokens)
            yield CompactionDone(
                reason=reason, ok=False, messages_before=held, messages_after=held,
                tokens_before=tokens, tokens_after=tokens, log_tokens=0,
            )
            return
        observation_message = self._observation_message()
        existing = self._observations_of(observation_message)
        existing_directives = self._directives_of(observation_message)
        # Fold every older message except the existing observation block (it is rebuilt).
        older = [message for message in self._conversation[:boundary] if message is not observation_message]
        # The log is excluded from the tail as well as the fold, since it is rebuilt and placed at the front below.
        recent = [message for message in self._conversation[boundary:] if message is not observation_message]
        if not older:
            # The boundary landed just past the log with nothing behind it, so there is nothing to fold.
            # Said rather than done in silence, so a deliberate request is answered either way.
            held = len(self._conversation)
            tokens = self._latest_context_tokens
            yield CompactionStarted(reason=reason, messages_before=held, tokens_before=tokens)
            yield CompactionDone(
                reason=reason, ok=False, messages_before=held, messages_after=held,
                tokens_before=tokens, tokens_after=tokens, log_tokens=0,
            )
            return
        tokens_before = self._latest_context_tokens
        messages_before = len(self._conversation)
        yield CompactionStarted(reason=reason,
            messages_before=messages_before,
            tokens_before=tokens_before,
        )
        # Both passes run against the same turns: what the work established, and what the person asked for.
        new_observations, new_directives = await asyncio.gather(
            self._fold_into_observations(older, existing),
            self._fold_into_directives(older, existing_directives),
        )
        await self._append_to_ledgers(new_observations, new_directives)
        if not new_observations:
            # Produced nothing parseable — leave history untouched rather than drop it.
            yield CompactionDone(reason=reason, ok=False,
                messages_before=messages_before,
                messages_after=messages_before,
                tokens_before=tokens_before,
            )
            return
        merged = [*existing, *new_observations]
        directives = [*existing_directives, *new_directives]
        compaction = self._global_configuration.compaction
        merged_tokens = count_tokens(lines(self._live(merged)))
        usable = self._usable_context()
        if usable > 0 and merged_tokens > compaction.condense_log_at_fraction * usable:
            consolidating = await self._reflect(merged)
            if consolidating:
                await self._append_to_ledgers(consolidating, [])
                merged = [*merged, *consolidating]
        # The person's own words are not carried through any more: their meaning is in the directive record,
        # and pasting the messages as well put the same instruction in the context twice.
        # Replace in place, because the conversation list object is shared with the executor's per-context store.
        self._conversation[:] = [
            self._build_observation_message(merged, directives),
            *_without_provider_reasoning(self._bounded_tail(recent)),
        ]
        # Occupancy no longer describes the conversation, so it is measured again rather than zeroed.
        self._latest_context_tokens = conversation_tokens(self._conversation)
        yield CompactionDone(reason=reason, ok=True,
            observations_added=len(new_observations),
            directives_added=len(new_directives),
            messages_before=messages_before,
            messages_after=len(self._conversation),
            tokens_before=tokens_before,
            tokens_after=self._latest_context_tokens,
            log_tokens=count_tokens(lines(self._live(merged))),
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
