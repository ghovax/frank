"""One turn, from an inbound message to a persisted, streamed result, as a typed pipeline of phases."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from a2a.server.agent_execution import RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Message, Part, Task, TaskState
from a2a.utils import new_task
from langchain_core.messages import messages_to_dict

from frank.base import telemetry as _telemetry
from frank.base.background_tasks import spawn_background_task
from frank.base.configuration import PromptLoader
from frank.base.serialization import conversation_snapshot_id
from frank.base.tuning import Tunable, active_tuning
from frank.protocol.errors import _safe_turn_error
from frank.protocol.events import ErrorEvent, StatusEvent
from frank.protocol.metadata import (
    METADATA_KEY,
    Metadata,
    PART_KIND,
    turn_metadata,
)
from frank.protocol.parts import (
    _all_attachments,
    _attachment_warning_event,
    _event_part,
    _ingest_incoming_file_parts,
    _input_response_payload,
    _structured_data_payloads,
    compose_turn_input,
    _text_part,
    _work_habits_acknowledgement_parts,
)
from frank.protocol.turn_record import TurnKind, TurnRecord
from frank.runtime.runtime import AgentRuntime
from frank.runtime.turn_events import SuspensionGate
from frank.worker.sink import _TurnEventSink

if TYPE_CHECKING:
    # For the annotation only: `session` imports this module, so a real import would close the cycle.
    from frank.worker.session import SessionExecutor

logger = logging.getLogger(__name__)

# Harness-authored, model-facing notes live as markdown in prompts/, not as string literals.
_PROMPTS = PromptLoader(Path(__file__).resolve().parent.parent / "runtime" / "prompts")


@dataclass
class _ContextState:
    """The session's live execution state, created on its first turn and dropped whole when it ends."""

    # Serializes the session's turns, so a message and a background wake never drive the runtime at once.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # The warm runtime, preserved across turns. None until the first turn builds it.
    runtime: Optional[Any] = None
    # After a turn ends with work in flight, this waits for each result and drives a turn to deliver it.
    resume_pump: Optional[asyncio.Task] = None
    # A turn is in flight, so a message can be injected into it rather than starting another.
    running: bool = False
    # The user stopped the session: no pump is armed, so a late job cannot wake a fresh turn.
    aborted: bool = False
    # A reset asked to drop the runtime while work was in flight, so the drop waits for idle.
    pending_reset: bool = False
    # A fork keeps its inherited prefix in an immutable daemon snapshot and persists only the
    # suffix it writes. Compaction that changes the prefix clears these fields and detaches it.
    inherited_snapshot_id: str = ""
    inherited_message_count: int = 0

@dataclass(frozen=True)
class _Ingested:
    """The parsed request: the turn's inputs and mode flags, threaded into the phase that follows."""
    message: Message
    user_text: str
    metadata: dict
    autonomous: bool
    compaction: bool
    # Opened only to remind this session it has not answered the one that created it.
    report_reminder: bool
    # Opened for a goal the session has not finished.
    goal_continuation: bool
    # The session that sent this message, empty for a person's and for a harness-initiated turn.
    peer_sender: str
    permission_mode: str
    requested_working_directory: str
    requested_worktree_strategy: str
    structured_payloads: list

    @property
    def from_outside(self) -> bool:
        """Whether somebody outside this session asked for this turn, rather than the session sending it to itself."""
        return not (self.autonomous or self.compaction or self.report_reminder or self.goal_continuation)


@dataclass(frozen=True)
class _Resolved:
    """The materialized task and the resume decision, produced by ``_resolve_task``."""
    ingested: _Ingested
    task: Task
    updater: TaskUpdater
    is_resume: bool
    resume_plans: dict
    resume_answers: dict


@dataclass(frozen=True)
class _Prepared:
    """The stood-up runtime and event sink, produced by ``_prepare_runtime``."""
    resolved: _Resolved
    runtime: AgentRuntime
    sink: _TurnEventSink


@dataclass(frozen=True)
class _ComposedTurn:
    """The model-facing input for this segment, produced by ``_compose_turn_input``."""
    prepared: _Prepared
    turn_input: Any
    as_system_note: bool


class _TurnRunner:
    """One run of `execute()` as a typed pipeline: each phase takes the last one's result and returns its own."""

    # A phase decided the turn is finished, so the spine stops after any owed teardown.
    _DONE = object()

    def __init__(
        self,
        executor: "SessionExecutor",
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        self._executor = executor
        self._request = context
        self._event_queue = event_queue
        # Teardown-visible state, defaulted so the `finally` runs even if setup throws before assigning it.
        self._runtime: AgentRuntime | None = None
        self._track_context_activity = False
        self._track_steerable_turn = False
        self._turn_has_images = False
        # Set only when the turn closed as completed: a failed or parked turn never got there.
        self._completed = False
        self._context_serialization_lock: asyncio.Lock | None = None
        self._on_turn_state = executor._on_turn_state
        # Filled in by _ingest / _resolve_task / _open_turn_span / _compose_turn_input.
        self._sink: _TurnEventSink | None = None

    async def run(self) -> None:
        # The ordering is enforced by the signatures: a phase cannot be called without its predecessor's result.
        ingested = await self._ingest()
        resolved = await self._resolve_task(ingested)
        if resolved is self._DONE:
            return
        assert isinstance(resolved, _Resolved)
        if await self._acknowledge_work_habits(resolved) is self._DONE:
            return
        await self._acquire_serialization_lock(resolved)
        # Runtime setup runs inside the try, so a missing credential is a clean `failed` rather than a torn stream.
        self._open_turn_span(resolved)
        try:
            prepared = await self._prepare_runtime(resolved)
            if prepared is self._DONE:
                return
            assert isinstance(prepared, _Prepared)
            if await self._reconcile_goal(prepared) is self._DONE:
                return
            if await self._run_compaction_turn(prepared) is self._DONE:
                return
            composed = await self._compose_turn_input(prepared)
            await self._stream_and_finalize(composed)
        except Exception as exception:  # noqa: BLE001 — surface any failure as A2A failed
            await self._fail(exception)
        finally:
            await self._teardown()

    # Collaborators shared across the phases below.

    async def _emit(self, part: Part, *, publish_stream_event: bool = True) -> None:
        await self._updater.update_status(TaskState.working, self._updater.new_agent_message([part]))
        if self._executor._on_stream_event is not None and publish_stream_event:
            self._executor._on_stream_event(self._task.context_id, part)

    def _checkpoint_messages(self, messages: list) -> tuple[list[dict[str, Any]], str]:
        serialized = messages_to_dict(messages)
        state = self._executor._contexts.get(self._task.context_id)
        if state is None or not state.inherited_snapshot_id:
            return serialized, ""
        boundary = state.inherited_message_count
        prefix = serialized[:boundary]
        if len(prefix) == boundary and conversation_snapshot_id(prefix) == state.inherited_snapshot_id:
            return serialized[boundary:], state.inherited_snapshot_id
        state.inherited_snapshot_id = ""
        state.inherited_message_count = 0
        return serialized, ""

    async def _save_runtime_conversation(self) -> None:
        # A safe-point snapshot of the conversation. A delegated turn keeps its throwaway one in memory.
        if self._runtime is not None:
            # Goal and tasks ride the same safe points, written atomically and only when they changed.
            session_state = self._runtime.dirty_session_snapshot()
            messages, inherited_snapshot_id = self._checkpoint_messages(self._runtime.conversation)
            await self._executor._turn_store.save_turn_state(
                self._task.context_id, self._task.id,
                messages, session_state, inherited_snapshot_id,
            )
            if session_state is not None:
                self._runtime.clear_session_dirty()

    async def _suspend_turn(self, interactions: list[SuspensionGate], plans: dict) -> bool:
        # Every pause is durable, so a session waiting on a human survives a daemon restart.
        return await self._executor._suspend_durable_segment(
            self._task, self._updater, interactions, plans, self._save_runtime_conversation
        )

    # The phases themselves.

    async def _publish_usage_snapshot(self) -> None:
        """Send the daemon what this turn learned about the account's limits. Never fatal to the turn."""
        from frank.base.subscription import get_usage_snapshot

        snapshot = get_usage_snapshot()
        if not snapshot:
            return
        try:
            await self._executor._turn_store.publish_usage(snapshot)
        except Exception:  # noqa: BLE001 — telemetry must not fail a turn
            logger.warning("could not publish the subscription usage snapshot", exc_info=True)

    async def _ingest(self) -> _Ingested:
        """Parse the request into the turn's inputs and mode flags, and store what teardown reads."""
        message = self._request.message
        if message is None:
            raise ValueError("Request context message is required.")
        self._message = message
        self._user_text = self._request.get_user_input()
        # Structured input arrives as DataParts beside the prose — today, attachments.
        self._structured_payloads = _structured_data_payloads(message)
        ingested_attachments = await _ingest_incoming_file_parts(message)
        if ingested_attachments:
            self._structured_payloads.append({PART_KIND: "attachments", "attachments": ingested_attachments})
        self._metadata = turn_metadata(message)
        # Dated on arrival and written back onto the message, since this is the one door every message comes through.
        if not self._metadata.get(Metadata.RECEIVED_AT):
            self._metadata[Metadata.RECEIVED_AT] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            message.metadata = {**(message.metadata or {}), METADATA_KEY: self._metadata}
        self._requested_working_directory = str(self._metadata.get(Metadata.WORKING_DIRECTORY, ""))
        self._requested_worktree_strategy = str(self._metadata.get(Metadata.WORKSPACE_STRATEGY, ""))
        self._permission_mode = str(self._metadata.get(Metadata.PERMISSION_MODE, ""))
        self._autonomous = bool(self._metadata.get(Metadata.AUTONOMOUS_RESUME))
        self._compaction = bool(self._metadata.get(Metadata.COMPACTION))
        self._report_reminder = bool(self._metadata.get(Metadata.REPORT_REMINDER))
        self._goal_continuation = bool(self._metadata.get(Metadata.GOAL_CONTINUATION))
        self._peer_sender = str(self._metadata.get(Metadata.PEER_SENDER, ""))
        return _Ingested(
            message=message,
            user_text=self._user_text,
            metadata=self._metadata,
            autonomous=self._autonomous,
            compaction=self._compaction,
            report_reminder=self._report_reminder,
            goal_continuation=self._goal_continuation,
            peer_sender=self._peer_sender,
            permission_mode=self._permission_mode,
            requested_working_directory=self._requested_working_directory,
            requested_worktree_strategy=self._requested_worktree_strategy,
            structured_payloads=self._structured_payloads,
        )

    async def _resolve_task(self, ingested: _Ingested) -> _Resolved | object:
        """Materialize the task, then record a resume answer or start a fresh turn."""
        message = ingested.message
        task = self._request.current_task
        if task is None:
            task = new_task(message)
            await self._event_queue.enqueue_event(task)
        self._task = task
        self._updater = TaskUpdater(self._event_queue, task.id, task.context_id)

        # An answer is recorded against the pending record; the turn resumes once every gate has one.
        input_response = _input_response_payload(message)
        self._is_resume = False
        self._resume_plans = {}
        self._resume_answers = {}
        if input_response is not None:
            if TurnRecord.from_metadata(task.metadata).pending is None:
                await self._updater.complete()
                return self._DONE
            ready = self._executor._record_pending_answer(task, input_response)
            await self._executor._turn_store.save(task)
            if self._executor._on_permission_state is not None:
                # Still awaiting while gates remain; cleared once this answer resumes.
                self._executor._on_permission_state(task.context_id, ready is None)
            if ready is None:
                await self._updater.update_status(
                    TaskState.input_required,
                    self._updater.new_agent_message([_event_part(StatusEvent(code="input_required"))]),
                    final=True,
                )
                return self._DONE
            self._resume_plans, self._resume_answers = ready
            cleared = TurnRecord.from_metadata(task.metadata)
            cleared.pending = None
            task.metadata = cleared.apply_to(task.metadata)
            await self._executor._turn_store.save(task)
            self._is_resume = True
        elif ingested.from_outside and self._executor._on_permission_state is not None:
            # A fresh user turn supersedes a prior pause, so drop the awaiting-input marker.
            self._executor._on_permission_state(task.context_id, False)
        return _Resolved(
            ingested=ingested,
            task=self._task,
            updater=self._updater,
            is_resume=self._is_resume,
            resume_plans=self._resume_plans,
            resume_answers=self._resume_answers,
        )

    async def _acknowledge_work_habits(self, resolved: _Resolved) -> object | None:
        """Emit the once-per-context work-habits acknowledgement, or report the turn failed."""
        task, ingested = resolved.task, resolved.ingested
        self._context_state = self._executor._context(task.context_id)
        should_acknowledge = await self._executor._claim_work_habits_acknowledgement(
            task.context_id,
            autonomous=ingested.autonomous or ingested.goal_continuation,
            compaction=ingested.compaction,
        )
        if should_acknowledge:
            try:
                for acknowledgement_part in _work_habits_acknowledgement_parts(task.id):
                    await self._emit(acknowledgement_part)
            except Exception as exception:
                logger.exception("work-habits acknowledgement failed: %s", exception)
                await self._updater.failed(self._updater.new_agent_message([_event_part(ErrorEvent(**_safe_turn_error(exception)))]))
                return self._DONE
        return None

    async def _acquire_serialization_lock(self, resolved: _Resolved) -> None:
        # Serialize the session's turns, so a message and a background wake never drive the runtime at once.
        self._context_serialization_lock = self._context_state.lock
        if self._context_serialization_lock is not None:
            await self._context_serialization_lock.acquire()

    def _open_turn_span(self, resolved: _Resolved) -> None:
        # One trace per turn, grouped by session, nesting under the peer that sent it when there is one.
        task, ingested = resolved.task, resolved.ingested
        self._turn_kind = (
            # The reminder is harness-initiated like a wake, and differs only in having nothing to deliver.
            TurnKind.AUTONOMOUS if ingested.autonomous or ingested.report_reminder or ingested.goal_continuation
            else TurnKind.COMPACTION if ingested.compaction
            # A peer's message is not the user speaking, or the model reads a report as an instruction.
            else TurnKind.PEER if ingested.peer_sender
            else TurnKind.USER
        )
        # Stamp the kind onto the task, so restart reconciliation reads a real field rather than guessing.
        stamped = TurnRecord.from_metadata(task.metadata)
        stamped.kind = self._turn_kind
        stamped.peer_sender = ingested.peer_sender
        task.metadata = stamped.apply_to(task.metadata)
        parent_context = _telemetry.context_from_traceparent((ingested.message.metadata or {}).get("traceparent", ""))
        self._turn_span_context = _telemetry.span("agent.turn", {
            "session.id": task.context_id,
            "frank.task.id": task.id,
            "frank.agent.name": self._executor._agent_name,
            "frank.turn.kind": self._turn_kind,
        }, parent_context)
        self._turn_span = self._turn_span_context.__enter__()

    async def _prepare_runtime(self, resolved: _Resolved) -> _Prepared | object:
        """Build or warm-fetch the runtime, register it, and stand up the event sink."""
        task = resolved.task
        # A wake with nothing left to deliver closes the task without a model call rather than an empty turn.
        if self._autonomous:
            existing_state = self._executor._contexts.get(task.context_id)
            existing_runtime = existing_state.runtime if existing_state is not None else None
            has_live_result = existing_runtime is not None and existing_runtime.has_completed_undelivered_jobs()
            has_stored_result = self._executor._job_store.has_undelivered_jobs(task.context_id, self._executor._agent_name)
            if not has_live_result and not has_stored_result:
                await self._updater.complete()
                return self._DONE

        self._track_context_activity = self._on_turn_state is not None
        self._track_steerable_turn = self._on_turn_state is not None
        # A turn opened from outside means the session is wanted working, so lift any prior Stop suppression.
        if resolved.ingested.from_outside:
            self._executor._context(task.context_id).aborted = False
        if self._track_context_activity and self._on_turn_state is not None:
            self._on_turn_state(task.context_id, True)
        if self._track_steerable_turn:
            self._executor._context(task.context_id).running = True

        await self._updater.start_work()

        workspace = self._executor._workspace(self._requested_working_directory)


        existing_state = self._executor._contexts.get(task.context_id)
        runtime = await self._executor._runtime_for(task.context_id, workspace)

        self._runtime = runtime
        self._executor._aborts[task.id] = runtime
        runtime.set_a2a_turn_id(task.id)

        # The consuming half of the turn-event catalog: it owns the text buffer, the span, and the wire translation.
        self._sink = _TurnEventSink(
            emit=self._emit,
            save_conversation=self._save_runtime_conversation,
            suspend=self._suspend_turn,
            telemetry_span=self._turn_span,
            model_identifier=lambda: self._runtime.effective_model_identifier if self._runtime is not None else "",
        )
        return _Prepared(resolved=resolved, runtime=runtime, sink=self._sink)

    async def _reconcile_goal(self, prepared: _Prepared) -> object | None:
        """Settle the goal against what opened this turn, now the runtime is built and the goal restored."""
        runtime = prepared.runtime
        if prepared.resolved.ingested.goal_continuation:
            goal = runtime.goal
            if goal is None or not goal.is_open:
                await self._updater.complete()
                return self._DONE
            runtime.note_goal_continuation()
            return None
        if prepared.resolved.ingested.from_outside:
            runtime.restore_goal_allowance()
        return None

    async def _run_compaction_turn(self, prepared: _Prepared) -> object | None:
        """A manual compaction runs no model turn: it folds the older history and emits the compaction parts."""
        if not prepared.resolved.ingested.compaction:
            return None
        async for compaction_event in prepared.runtime.compact(reason="manual"):
            await prepared.sink.emit_compaction(compaction_event)
        await self._save_runtime_conversation()
        await self._updater.complete()
        return self._DONE

    async def _compose_turn_input(self, prepared: _Prepared) -> _ComposedTurn:
        """Build the model-facing input: a framing note, structured attachments, or plain user text."""
        runtime = prepared.runtime
        self._as_system_note = self._autonomous or self._report_reminder or self._goal_continuation
        if self._goal_continuation:
            # The goal as the turn's opening message, delivered as a reminder so nothing claims a person asked.
            goal = runtime.goal
            requirements = "\n".join(f"- {requirement}" for requirement in (goal.requirements if goal else []))
            self._turn_input = _PROMPTS.load("goal_continuation", {
                "goal": goal.text if goal else "",
                "requirements": requirements,
                # From the same setting anything else reads, so the number told and the number configured cannot drift.
                "blocked_turns": active_tuning().amount(Tunable.goal_blocked_turns),
            })
        elif self._report_reminder:
            # A reminder, never user prose: this is the harness speaking, not the person the session works for.
            self._turn_input = _PROMPTS.load("report_reminder_note", {"parent": self._executor._parent})
        elif self._autonomous:
            # The wake carries no prose, so the framing note is supplied here and delivered as a reminder.
            self._turn_input = _PROMPTS.load("background_resume_note", {})
        elif self._structured_payloads:
            # Give the tools read access to exactly the attached files, which usually live under a denied directory.
            if runtime is not None:
                runtime.note_attachments([
                    str(attachment.get("path") or "")
                    for attachment in _all_attachments(self._structured_payloads)
                ])
            # Paths always ride as a text block; images are inlined only where the model advertises vision.
            model_identifier = runtime.effective_model_identifier if runtime is not None else ""
            self._turn_input, images_not_inlined = compose_turn_input(
                self._user_text, self._structured_payloads, model_identifier,
            )
            self._turn_has_images = isinstance(self._turn_input, list)
            if images_not_inlined:
                await self._emit(_event_part(
                    _attachment_warning_event(images_not_inlined, model_identifier)
                ))
        else:
            self._turn_input = self._user_text
        # Attachments reach the model through `_structured_payloads`, which is where the image blocks are built.
        return _ComposedTurn(
            prepared=prepared,
            turn_input=self._turn_input,
            as_system_note=self._as_system_note,
        )

    async def _stream_and_finalize(self, composed: _ComposedTurn) -> None:
        """Drive the runtime's stream through the sink, then close the task as completed or canceled."""
        resolved = composed.prepared.resolved
        # A resume drives from the durable checkpoint, a fresh turn from this segment's input, through one loop.
        event_source = (
            composed.prepared.runtime.resume_stream(resolved.resume_plans, resolved.resume_answers)
            if resolved.is_resume
            else composed.prepared.runtime.stream(composed.turn_input, as_system_note=composed.as_system_note)
        )
        async for event in event_source:
            if await self._sink.handle(event):
                return

        await self._sink.flush()

        if self._sink.final_text.strip():
            await self._updater.add_artifact(
                [_text_part(self._sink.final_text, f"artifact-result:{self._task.id}")],
                name="result",
                last_chunk=True,
            )
        await self._save_runtime_conversation()
        if self._sink.stop_reason == "cancelled":
            # Stop ends the task as canceled, so the transcript reads it honestly as a stopped turn.
            await self._updater.cancel()
        else:
            await self._updater.complete()
            self._completed = True

    async def _fail(self, exception: Exception) -> None:
        await self._save_runtime_conversation()
        # Log the real exception, but show the user a safe category rather than raw exception text.
        logger.exception("agent turn failed: %s", exception)
        await self._updater.failed(self._updater.new_agent_message(
            [_event_part(ErrorEvent(**_safe_turn_error(exception, had_images=self._turn_has_images)))]
        ))

    async def _teardown(self) -> None:
        task = self._task
        with suppress(Exception):
            self._turn_span_context.__exit__(None, None, None)
        self._executor._aborts.pop(task.id, None)
        # None when the session was deleted mid-turn: a torn-down context must not be re-persisted or re-armed.
        state = self._executor._contexts.get(task.context_id)
        # Persist the conversation only when there is something to save, or a no-op wake would clear it.
        if state is not None and (
            self._runtime is not None or task.context_id in self._executor._conversations
        ):
            messages = self._runtime.conversation if self._runtime is not None else self._executor._conversations.get(task.context_id, [])
            # Goal and tasks persist atomically beside the checkpoint, so the two can never drift apart.
            session_state = self._runtime.dirty_session_snapshot() if self._runtime is not None else None
            checkpoint_messages, inherited_snapshot_id = self._checkpoint_messages(messages)
            await self._executor._turn_store.save_turn_state(
                task.context_id, task.id, checkpoint_messages, session_state, inherited_snapshot_id,
            )
            if session_state is not None and self._runtime is not None:
                self._runtime.clear_session_dirty()
            # The rate-limit reading, captured here because the headers only ride on a model call in this process.
            await self._publish_usage_snapshot()
        # Stop accepting steering, then discard what arrived too late: the client re-delivers it as a fresh turn.
        if self._track_steerable_turn and state is not None:
            state.running = False
        if state is not None and state.runtime is not None:
            state.runtime.discard_pending_steering()
        if self._track_context_activity and self._on_turn_state is not None:
            self._on_turn_state(task.context_id, False)
        if self._context_serialization_lock is not None:
            self._context_serialization_lock.release()
        # Arm a pump for work still in flight, passing the runtime this turn used rather than a cache lookup.
        self._executor._arm_resume_pump(task.context_id, self._runtime)
        await self._maybe_continue_goal()
        self._maybe_nudge_to_report()

    async def _maybe_continue_goal(self) -> None:
        """Open another turn when this one ended with the goal unfinished, which is what makes a goal outlive a turn."""
        if not self._completed:
            return
        state = self._executor._contexts.get(self._task.context_id)
        if state is None or state.aborted:
            return
        runtime = self._runtime
        goal = runtime.goal if runtime is not None else None
        if goal is None or not goal.is_open:
            return
        if runtime is not None and runtime.has_pending_jobs():
            return
        if goal.continuations >= active_tuning().amount(Tunable.goal_continuation_turns):
            # Written now: parking is what stops the session, and a stop only in memory is one a restart undoes.
            runtime.park_goal()
            await self._executor._persist_session_state(self._task.context_id, runtime)
            return
        spawn_background_task(self._executor.continue_goal(self._task.context_id))

    def _maybe_nudge_to_report(self) -> None:
        """Remind the session once if a completed turn left its creator with no answer. A nudge, not a gate."""
        peers = getattr(self._executor, "_peers", None)
        if peers is None or not self._completed or self._report_reminder:
            return
        if self._turn_kind == TurnKind.USER:
            return
        if not getattr(peers, "_parent_session", "") or peers.reported_to_parent:
            return
        if self._executor._nudged_to_report:
            return
        spawn_background_task(self._executor.nudge_to_report(self._task.context_id))
