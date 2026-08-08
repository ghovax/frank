"""The runtime's compaction concern: when to fold a conversation, and the observation log it folds into."""

from __future__ import annotations

import asyncio
import logging
from itertools import accumulate, takewhile

from langmesh.base.message_content import forget_carried_reasoning, message_text
from langmesh.base.tuning import Tunable, active_tuning, count_tokens
from langmesh.runtime.internals import (
    DirectiveBatch,
    ObservationBatch,
    conversation_tokens,
    emit_structured,
    message_tokens,
)
from langmesh.runtime.turn_events import CompactionDone, CompactionStarted, TurnEvent
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from typing import AsyncIterator
from langmesh.base.errors import log_fields
from langmesh.base.serialization import content_address, lines


logger = logging.getLogger(__name__)


def _without_provider_reasoning(messages: list) -> list:
    """The same messages with the provider-native reasoning cut out, since the turns it explained are gone."""
    # A sweep for its effect, not a transformation: `forget_carried_reasoning` edits each message in place.
    for message in messages:
        forget_carried_reasoning(message)
    return messages


class _CompactsContext:
    """Keeping a conversation inside its window: what each exchange establishes is recorded, then its turns are dropped."""

    async def _emit_batch(self, schema, request: list, what: str):
        """One structured call against this runtime's model, which is the shared pass every fold is built on."""
        return await emit_structured(
            self._llm, schema, request, what, active_tuning().amount(Tunable.compaction_attempts)
        )

    async def _emit_observations(self, request: list) -> list:
        """Run one observer call and read its entries from the tool call it makes."""
        batch = await self._emit_batch(ObservationBatch, request, "observer")
        return list(batch.observations) if batch else []

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
        """The record as a later pass is shown it: enough to judge an entry, date it and name it, not its whole text."""
        fields = (
            ("kind", "summary") if entries and "summary" in entries[0] else ("category", "claim")
        )
        # When it was learned, because deciding what a new finding replaces depends on which came first.
        return [
            {
                "id": entry.get("id"),
                "learned": entry.get("written_at", ""),
                **{name: entry.get(name) for name in fields},
            }
            for entry in entries
        ]

    @staticmethod
    def _readable(entries: list[dict]) -> list[dict]:
        """An entry as the model reads it, without the bookkeeping that only this code needs."""
        hidden = {"exchange", "written_at"}
        return [
            {name: value for name, value in entry.items() if name not in hidden}
            | {"learned": entry.get("written_at", "")}
            for entry in entries
        ]

    def _build_observation_message(self, observations: list[dict], directives: list[dict]):
        """The record the agent reads: both ledgers, rendered as the turns they describe leave the conversation."""
        return self._reminder_message(
            self._prompt_loader.load(
                "observation_log",
                {
                    "observations": lines(self._readable(self._live(observations))),
                    "directives": lines(
                        self._readable(
                            [
                                one
                                for one in self._live(directives)
                                if one.get("still_binding", True)
                            ]
                        )
                    ),
                },
            ),
            marks={"observation_log": True, "observations": observations, "directives": directives},
        )

    def _usable_context(self) -> int:
        """How much of the window a conversation may occupy, leaving room for the answer and for the fold itself."""
        window = self._context_window
        if window <= 0:
            return 0
        return max(
            0, window - int(window * self._global_configuration.compaction.output_reserve_fraction)
        )

    def _recent_working_set(self, reason: str = "automatic") -> int:
        """The tail kept verbatim rather than folded, as a share of the usable window so it scales with the model."""
        fraction = self._global_configuration.compaction.recent_working_set_fraction
        budget = int(self._usable_context() * fraction)
        if reason != "manual":
            return budget
        # Asked for deliberately, so the share is of what is there: a small conversation still has a tail to cut.
        return min(budget, int(conversation_tokens(self._conversation) * fraction))

    def _at_folding_threshold(self) -> bool:
        """Whether the live context has grown enough that folding is worth the prompt cache it throws away."""
        usable = self._usable_context()
        return usable > 0 and self._latest_context_tokens >= (
            self._global_configuration.compaction.reclaim_at_fraction * usable
        )

    def _should_compact(self) -> bool:
        """The automatic trigger, measured against the usable window, unless a strategy answers it instead."""
        if self._compaction is not None:
            return bool(self._compaction.should_compact(self._compaction_state()))
        compaction = self._global_configuration.compaction
        if not compaction.automatic or not self._at_folding_threshold():
            return False
        return len(self._bounded_tail(self._conversation)) < len(self._conversation)

    def _bounded_tail(self, recent: list) -> list:
        """The newest turns that fit the budget, taken whole: recency is not smallness, and none is cut in half."""
        budget = self._recent_working_set()
        if budget <= 0 or not recent:
            return recent
        # How many of the newest turns fit: running totals from the end, taken while they stay inside the budget.
        running = accumulate(message_tokens(message) for message in reversed(recent))
        fitting = sum(1 for _ in takewhile(lambda total: total <= budget, running))
        # A tool result without the call it answers is not a conversation, so the tail never begins on one.
        keep_from = next(
            (
                index
                for index in range(len(recent) - fitting, len(recent))
                if not isinstance(recent[index], ToolMessage)
            ),
            len(recent),
        )
        if keep_from:
            logger.info(
                "recent turns bounded to their budget",
                extra=log_fields(dropped=keep_from, kept=len(recent) - keep_from, budget=budget),
            )
        return recent[keep_from:]

    def _exchange_of(self, opening) -> str:
        """What names an exchange: the message that opened it, so its entries can be matched to it later."""
        return content_address(message_text(opening))

    def _current_exchange(self) -> list:
        """The unit just completed: the person's last message and everything the agent did after it."""
        opening = next(
            (
                index
                for index in range(len(self._conversation) - 1, -1, -1)
                if isinstance(self._conversation[index], HumanMessage)
                and not self._conversation[index].additional_kwargs.get("reminder")
            ),
            None,
        )
        if opening is None:
            return []
        # The harness's own notes are cut out: the turn context, the confinement profile and the system
        # reminders are scaffolding, and an observer given them records the scaffolding as a finding.
        return [
            message
            for message in self._conversation[opening:]
            if not message.additional_kwargs.get("reminder")
            or message is self._conversation[opening]
        ]

    def _exchanges_present(self) -> set[str]:
        """Every exchange still visible in the conversation, whose entries therefore need not be repeated."""
        return {
            self._exchange_of(message)
            for message in self._conversation
            if isinstance(message, HumanMessage) and not message.additional_kwargs.get("reminder")
        }

    async def observe_exchange(self) -> None:
        """Record what the exchange just established, stored now and shown only once its turns are gone."""
        exchange = self._current_exchange()
        if len(exchange) < 2 or self._turn_store is None:
            return
        tag = self._exchange_of(exchange[0])
        recorded = await self._ledger("observations")
        if any(entry.get("exchange") == tag for entry in recorded):
            return  # already observed: a turn can end more than once
        asked = await self._ledger("directives")
        observations, directives = await asyncio.gather(
            self._fold_into_observations(exchange, recorded, asked),
            self._fold_into_directives(exchange, asked),
        )
        await self._append_to_ledgers(
            [{**entry, "exchange": tag} for entry in observations],
            [{**entry, "exchange": tag} for entry in directives],
        )

    async def _ledger(self, ledger: str) -> list[dict]:
        """A session's ledger as it stands, or nothing where there is no store to ask."""
        if self._turn_store is None:
            return []
        try:
            return await self._turn_store.ledger_entries(self._session_id, ledger)
        except Exception:  # noqa: BLE001 — a record that cannot be read is not a turn that cannot run
            logger.warning("could not read the %s ledger", ledger, exc_info=True)
            return []

    async def carried_record(self):
        """The record to carry into this turn: what was established in exchanges the model can no longer see."""
        if self._turn_store is None:
            return None
        present = self._exchanges_present()
        observations = [
            entry
            for entry in await self._ledger("observations")
            if entry.get("exchange") not in present
        ]
        directives = [
            entry
            for entry in await self._ledger("directives")
            if entry.get("exchange") not in present and entry.get("still_binding", True)
        ]
        if not observations and not directives:
            return None
        return self._build_observation_message(observations, directives)

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

    async def _fold_into_observations(
        self, older: list, existing: list[dict], asked: list[dict]
    ) -> list[dict]:
        """New entries from these messages, with both records shown so it writes neither what it nor the other holds."""
        instructions = self._prompt_loader.load(
            "observer",
            {
                "existing_observations": lines(self._claims(self._live(existing))),
                # The other ledger too: an entry cannot be left to a record it was never shown.
                "existing_directives": lines(self._claims(self._live(asked))),
            },
        )
        entries = await self._emit_observations(
            [
                SystemMessage(content=instructions),
                *older,
                HumanMessage(content=self._prompt_loader.load("observe_now", {})),
            ]
        )
        return self._identified(entries)

    async def _fold_into_directives(self, older: list, existing: list[dict]) -> list[dict]:
        """What the person asked for in these turns, in their meaning rather than their words."""
        spoken = [
            message
            for message in older
            if isinstance(message, HumanMessage) and not message.additional_kwargs.get("reminder")
        ]
        if not spoken:
            return []
        shown = lines(self._claims(self._live(existing)))
        instructions = self._prompt_loader.load("directives", {"existing_directives": shown})
        entries = await self._emit_batch(
            DirectiveBatch,
            [
                SystemMessage(content=instructions),
                *spoken,
                HumanMessage(content=self._prompt_loader.load("directives_now", {})),
            ],
            "directive",
        )
        return self._identified(getattr(entries, "directives", []) if entries else [])

    async def compact(self, reason: str = "manual") -> AsyncIterator[TurnEvent]:
        """Reclaim the window by dropping what is past the tail, which the exchange records have already captured."""
        # A supplied strategy replaces the recording entirely, while the events around it stay the runtime's.
        if self._compaction is not None:
            state = self._compaction_state(reason)
            messages_before = len(self._conversation)
            tokens_before = self._latest_context_tokens
            yield CompactionStarted(
                reason=reason,
                messages_before=messages_before,
                tokens_before=tokens_before,
            )
            self._conversation[:] = _without_provider_reasoning(
                await self._compaction.compact(state)
            )
            yield CompactionDone(
                reason=reason,
                ok=True,
                messages_before=messages_before,
                messages_after=len(self._conversation),
                tokens_before=tokens_before,
                tokens_after=self._latest_context_tokens,
            )
            return
        # Each exchange was recorded when it closed, so all that is left here is the discarding.
        messages_before = len(self._conversation)
        tokens_before = self._latest_context_tokens
        yield CompactionStarted(
            reason=reason, messages_before=messages_before, tokens_before=tokens_before
        )
        kept = self._bounded_tail(self._conversation)
        if len(kept) >= messages_before:
            # Everything already fits, so a deliberate request is answered rather than met with silence.
            yield CompactionDone(
                reason=reason,
                ok=False,
                messages_before=messages_before,
                messages_after=messages_before,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                log_tokens=0,
            )
            return
        self._conversation[:] = _without_provider_reasoning(kept)
        self._latest_context_tokens = conversation_tokens(self._conversation)
        recorded = await self._ledger("observations")
        yield CompactionDone(
            reason=reason,
            ok=True,
            messages_before=messages_before,
            messages_after=len(self._conversation),
            tokens_before=tokens_before,
            tokens_after=self._latest_context_tokens,
            log_tokens=count_tokens(lines(recorded)),
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
        return list(state.messages[-self._keep * 2 :])


__all__ = ["KeepRecentTurns"]
