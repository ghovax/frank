"""One turn, from an inbound message to a persisted, streamed result.

The runner is the per-turn state machine, and its phases form a typed pipeline: each takes
the previous phase's result and returns its own, so the ordering is a type constraint rather
than an unwritten rule. Once the turn takes the session lock every exit — normal, early, or
failed — runs through one teardown exactly once.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import DataPart, Message, Part, Role, Task, TaskState
from a2a.utils import new_task
from langchain_core.messages import messages_to_dict

from xeac.base import telemetry as _telemetry
from xeac.base.configuration import PromptLoader
from xeac.base.permission_mode import PermissionMode
from xeac.protocol.errors import _safe_turn_error
from xeac.protocol.events import ErrorEvent, StatusEvent
from xeac.protocol.metadata import (
    AUTONOMOUS_RESUME_KIND,
    COMPACTION_KIND,
    INPUT_RESPONSE_KIND,
    Metadata,
    PART_KIND,
    envelope_part,
    turn_metadata,
    turn_metadata_envelope,
)
from xeac.protocol.parts import (
    _all_attachments,
    _artifact_event_payload,
    _attachment_warning_event,
    _event_part,
    _image_attachments,
    _image_content_block,
    _ingest_incoming_file_parts,
    _input_response_payload,
    _model_supports_vision,
    _structured_data_payloads,
    _text_part,
    _work_habits_acknowledgement_parts,
)
from xeac.protocol.turn_record import PendingInteraction, ToolGate, TurnKind, TurnRecord
from xeac.runtime.annotation_stamping import annotation_image_blocks, normalize_annotation_payloads
from xeac.runtime.runtime import AgentRuntime
from xeac.runtime.turn_events import SuspensionGate
from xeac.worker.sink import _TurnEventSink

logger = logging.getLogger(__name__)

# Harness-authored, model-facing notes live as markdown in prompts/, not as string literals.
_PROMPTS = PromptLoader(Path(__file__).resolve().parent.parent / "runtime" / "prompts")


@dataclass(frozen=True)
class _Ingested:
    """The parsed request — the turn's inputs and mode flags — produced by ``_ingest``. Each
    later phase takes the result of the phase before it as a required argument, so the ordering
    is a type constraint (a phase literally cannot be called without its predecessor's output)
    rather than the unwritten rule that a shared instance-var machine leaves implicit."""
    message: Message
    user_text: str
    metadata: dict
    autonomous: bool
    compaction: bool
    permission_mode: str
    requested_working_directory: str
    requested_workspace_strategy: str
    artifact_payload: Optional[dict]
    structured_payloads: list


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
    """One run of the executor's ``execute()`` — the per-turn state machine made explicit.

    The phases form a **typed pipeline**: each takes the previous phase's typed result and
    returns its own — ``_ingest`` → :class:`_Ingested`, ``_resolve_task`` → :class:`_Resolved`,
    ``_prepare_runtime`` → :class:`_Prepared`, ``_compose_turn_input`` → :class:`_ComposedTurn` —
    so the ordering is a type constraint (``_prepare_runtime`` cannot be called before
    ``_resolve_task`` because it *requires* a ``_Resolved``), not the unwritten rule a shared
    instance-var machine leaves implicit. The data that flows phase→phase rides those results;
    the *lifecycle* state that the shared collaborators (``_emit``/``_suspend_turn``/
    ``_save_runtime_conversation``) and the single ``finally`` teardown read — the task, updater,
    runtime, sink, telemetry span, and a fistful of teardown flags — stays as instance
    attributes, because teardown must run on a *partial* completion, reading whatever was set so
    far. The sequence: ingest the request, resolve the task and any resume answer, acknowledge
    work habits, serialize on the per-context lock, build the runtime, compose the model input,
    stream, and finalize — with one teardown that always runs once the turn has taken the lock.

    The phases up to and including the lock acquisition can short-circuit the turn
    before any teardown is owed (a stale resume answer, a partial answer, a failed
    work-habits acknowledgement); they signal that by returning :data:`_DONE`. Once the
    lock and span are taken, every exit — normal, early (an autonomous no-op wake, a
    manual compaction), or failed — runs through the ``finally`` teardown exactly once.

    Instances are single-use: ``execute()`` builds one per call and awaits ``run()``.
    """

    # A phase decided the turn is finished, so the spine should stop after any owed
    # teardown runs.
    _DONE = object()

    def __init__(
        self,
        executor: "SessionExecutor",
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        self._ex = executor
        self._request = context
        self._event_queue = event_queue
        # Teardown-visible state, initialized to safe defaults so the ``finally`` can run
        # even if runtime setup throws before assigning them.
        self._runtime: AgentRuntime | None = None
        self._track_context_activity = False
        self._track_steerable_turn = False
        self._turn_has_images = False
        self._context_serialization_lock: asyncio.Lock | None = None
        self._on_turn_state = executor._on_turn_state
        # Filled in by _ingest / _resolve_task / _open_turn_span / _compose_turn_input.
        self._sink: _TurnEventSink | None = None

    async def run(self) -> None:
        # A typed pipeline: each phase takes the previous phase's typed result and returns its
        # own, so the ordering is enforced by the signatures — ``_prepare_runtime`` cannot be
        # called before ``_resolve_task`` because it requires a ``_Resolved``. The phases still
        # populate the lifecycle instance vars the shared collaborators and the ``finally``
        # teardown read (the task, updater, runtime, sink, flags), because teardown must run on
        # a *partial* completion; the typed results carry the data that flows phase→phase.
        ingested = await self._ingest()
        resolved = await self._resolve_task(ingested)
        if resolved is self._DONE:
            return
        assert isinstance(resolved, _Resolved)
        if await self._acknowledge_work_habits(resolved) is self._DONE:
            return
        await self._acquire_serialization_lock(resolved)
        # The runtime setup — building the agent runtime and its model client — runs
        # inside the try so any failure (e.g. missing API credentials) is surfaced as a
        # clean A2A `failed` status rather than escaping and tearing down the SSE stream
        # mid-flight. From here the teardown is owed on every exit.
        self._open_turn_span(resolved)
        try:
            prepared = await self._prepare_runtime(resolved)
            if prepared is self._DONE:
                return
            assert isinstance(prepared, _Prepared)
            if await self._run_compaction_turn(prepared) is self._DONE:
                return
            composed = await self._compose_turn_input(prepared)
            await self._stream_and_finalize(composed)
        except Exception as exception:  # noqa: BLE001 — surface any failure as A2A failed
            await self._fail(exception)
        finally:
            await self._teardown()

    # -- collaborators shared across phases --------------------------------------

    async def _emit(self, part: Part, *, publish_stream_event: bool = True) -> None:
        await self._updater.update_status(TaskState.working, self._updater.new_agent_message([part]))
        if self._ex._on_stream_event is not None and publish_stream_event:
            self._ex._on_stream_event(self._task.context_id, part)

    async def _save_runtime_conversation(self) -> None:
        # A safe-point (or end-of-turn) snapshot of the model-facing conversation to the
        # per-context checkpoint. Delegated turns keep their throwaway conversation in
        # memory (their pause is ephemeral, and their conversation is not the context's),
        # so they never write the context checkpoint.
        if self._runtime is not None:
            # The agent's goal and task list ride the same safe points as the conversation and
            # are written atomically with it (one transaction), persisted only when they changed
            # so they are as durable as the transcript without a write on every checkpoint. The
            # dirty flag is cleared only after the write commits — a failed write loses nothing.
            session_state = self._runtime.dirty_session_snapshot()
            await self._ex._task_store.save_turn_state(
                self._task.context_id, self._task.id,
                messages_to_dict(self._runtime.conversation), session_state,
            )
            if session_state is not None:
                self._runtime.clear_session_dirty()

    async def _suspend_turn(self, interactions: list[SuspensionGate], plans: dict) -> bool:
        # Every pause is durable: persist the pending interactions and the checkpoint, then
        # close this segment as input-required so a later answer rebuilds from the checkpoint
        # and resumes. A session that is waiting on a human survives a daemon restart.
        return await self._ex._suspend_durable_segment(
            self._task, self._updater, interactions, plans, self._save_runtime_conversation
        )

    # -- phases ------------------------------------------------------------------

    async def _ingest(self) -> _Ingested:
        """Parse the request message into the turn's inputs and mode flags. Returns them as a
        typed ``_Ingested`` (threaded into the next phase) and also stores the lifecycle bits
        (`_message`, `_metadata`, …) the shared collaborators and teardown read."""
        message = self._request.message
        if message is None:
            raise ValueError("Request context message is required.")
        self._message = message
        self._user_text = self._request.get_user_input()
        # An artifact interaction arrives as a structured DataPart. A normal interaction
        # becomes the turn's JSON input; a render_error is reframed below (once the
        # runtime's prompt loader exists) into a behind-the-scenes self-realization note
        # the model repairs as its own output.
        self._artifact_payload = _artifact_event_payload(message)
        self._structured_payloads = _structured_data_payloads(message)
        ingested_attachments = await _ingest_incoming_file_parts(message)
        if ingested_attachments:
            self._structured_payloads.append({PART_KIND: "attachments", "attachments": ingested_attachments})
        self._metadata = turn_metadata(message)
        self._requested_working_directory = str(self._metadata.get(Metadata.WORKING_DIRECTORY, ""))
        self._requested_workspace_strategy = str(self._metadata.get(Metadata.WORKSPACE_STRATEGY, ""))
        self._permission_mode = str(self._metadata.get(Metadata.PERMISSION_MODE, ""))
        self._autonomous = bool(self._metadata.get(Metadata.AUTONOMOUS_RESUME))
        self._compaction = bool(self._metadata.get(Metadata.COMPACTION))
        return _Ingested(
            message=message,
            user_text=self._user_text,
            metadata=self._metadata,
            autonomous=self._autonomous,
            compaction=self._compaction,
            permission_mode=self._permission_mode,
            requested_working_directory=self._requested_working_directory,
            requested_workspace_strategy=self._requested_workspace_strategy,
            artifact_payload=self._artifact_payload,
            structured_payloads=self._structured_payloads,
        )

    async def _resolve_task(self, ingested: _Ingested) -> _Resolved | object:
        """Materialize the task, then either record a resume answer or start a fresh
        turn. Returns ``_DONE`` when the request is fully handled without streaming (a
        stale or partial answer), else the :class:`_Resolved` the next phases thread."""
        message, metadata = ingested.message, ingested.metadata
        task = self._request.current_task
        if task is None:
            task = new_task(message)
            await self._event_queue.enqueue_event(task)
        self._task = task
        self._updater = TaskUpdater(self._event_queue, task.id, task.context_id)

        # An input-required answer is recorded against the task's pending-interaction
        # record; once every gate is answered, the turn rebuilds the runtime from the
        # persisted checkpoint and drives the next segment. A partial answer leaves the
        # task input-required for the next one; an answer for a task that is not paused is
        # a no-op acknowledgement.
        input_response = _input_response_payload(message)
        self._is_resume = False
        self._resume_plans = {}
        self._resume_answers = {}
        if input_response is not None:
            if TurnRecord.from_metadata(task.metadata).pending is None:
                await self._updater.complete()
                return self._DONE
            ready = self._ex._record_pending_answer(task, input_response)
            await self._ex._task_store.save(task)
            if self._ex._on_permission_state is not None:
                # Still awaiting while gates remain; cleared once this answer resumes.
                self._ex._on_permission_state(task.context_id, ready is None)
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
            await self._ex._task_store.save(task)
            self._is_resume = True
        elif not ingested.autonomous and not ingested.compaction and self._ex._on_permission_state is not None:
            # A fresh user turn supersedes any prior input-required pause for this context
            # (the runtime closes the dangling checkpoint), so drop the awaiting-input marker.
            self._ex._on_permission_state(task.context_id, False)
        return _Resolved(
            ingested=ingested,
            task=self._task,
            updater=self._updater,
            is_resume=self._is_resume,
            resume_plans=self._resume_plans,
            resume_answers=self._resume_answers,
        )

    async def _acknowledge_work_habits(self, resolved: _Resolved) -> object | None:
        """Emit the once-per-context work-habits acknowledgement. Returns ``_DONE`` if
        the acknowledgement itself failed (the turn is reported failed)."""
        task, ingested = resolved.task, resolved.ingested
        self._context_state = self._ex._context(task.context_id)
        should_acknowledge = await self._ex._claim_work_habits_acknowledgement(
            task.context_id,
            autonomous=ingested.autonomous,
            compaction=ingested.compaction,
        )
        if should_acknowledge:
            try:
                for acknowledgement_part in _work_habits_acknowledgement_parts(task.id):
                    await self._emit(acknowledgement_part)
            except Exception as exception:
                logger.exception("Work-habits acknowledgement failed: %s", exception)
                await self._updater.failed(self._updater.new_agent_message([_event_part(ErrorEvent(**_safe_turn_error(exception)))]))
                return self._DONE
        return None

    async def _acquire_serialization_lock(self, resolved: _Resolved) -> None:
        # Serialize the session's turns so a user message and an autonomous background wake
        # never drive the one runtime concurrently.
        self._context_serialization_lock = self._context_state.lock
        if self._context_serialization_lock is not None:
            await self._context_serialization_lock.acquire()

    def _open_turn_span(self, resolved: _Resolved) -> None:
        # The turn is one trace, grouped by the session; a caller's traceparent (in the
        # message metadata) makes this turn nest under the peer that sent it.
        task, ingested = resolved.task, resolved.ingested
        self._turn_kind = (
            TurnKind.AUTONOMOUS if ingested.autonomous
            else TurnKind.COMPACTION if ingested.compaction
            else TurnKind.USER
        )
        # Stamp the kind onto the task so the restart reconciliation reads a real field:
        # an input-required pause is preserved, and any
        # mid-execution turn are failed. Persisted with the head on the next save.
        stamped = TurnRecord.from_metadata(task.metadata)
        stamped.kind = self._turn_kind
        task.metadata = stamped.apply_to(task.metadata)
        parent_context = _telemetry.context_from_traceparent((ingested.message.metadata or {}).get("traceparent", ""))
        self._turn_span_context = _telemetry.span("agent.turn", {
            "session.id": task.context_id,
            "xeac.task.id": task.id,
            "xeac.agent.name": self._ex._agent_name,
            "xeac.turn.kind": self._turn_kind,
        }, parent_context)
        self._turn_span = self._turn_span_context.__enter__()

    async def _prepare_runtime(self, resolved: _Resolved) -> _Prepared | object:
        """Build (or warm-fetch) the runtime, register it, and stand up the event sink.
        Returns ``_DONE`` for an autonomous wake that has nothing left to deliver, else the
        :class:`_Prepared` the streaming phases thread."""
        task, metadata = resolved.task, resolved.ingested.metadata
        # An autonomous wake with nothing left to deliver — a concurrent user turn already
        # drained the result while this one waited on the lock — is a no-op: close the task
        # without a model call rather than emit an empty turn. The finally still runs on
        # this early return, releasing the lock.
        if self._autonomous:
            existing_state = self._ex._contexts.get(task.context_id)
            existing_runtime = existing_state.runtime if existing_state is not None else None
            has_live_result = existing_runtime is not None and existing_runtime.has_completed_undelivered_jobs()
            has_stored_result = get_background_job_store().has_undelivered_jobs(task.context_id, self._ex._agent_name)
            if not has_live_result and not has_stored_result:
                await self._updater.complete()
                return self._DONE

        self._track_context_activity = self._on_turn_state is not None
        self._track_steerable_turn = self._on_turn_state is not None
        # A real user turn (not an autonomous wake or a compaction) means the user wants
        # the agent working again, so lift any prior Stop suppression — future background
        # completions may once more arm a resume pump.
        if not self._autonomous and not self._compaction:
            self._ex._context(task.context_id).aborted = False
        if self._track_context_activity and self._on_turn_state is not None:
            self._on_turn_state(task.context_id, True)
        if self._track_steerable_turn:
            self._ex._context(task.context_id).running = True

        await self._updater.start_work()

        workspace = await self._ex._workspace_for(
            context_id=task.context_id,
            requested_working_directory=self._requested_working_directory,
            requested_workspace_strategy=self._requested_workspace_strategy,
            requested_permission_mode=self._permission_mode,
            first_message=self._user_text,
            metadata=metadata,
        )
        if self._ex._ensure_mcp_servers is not None and workspace.source_working_directory:
            await self._ex._ensure_mcp_servers(workspace.source_working_directory)

        existing_state = self._ex._contexts.get(task.context_id)
        is_new_context = existing_state is None or existing_state.runtime is None
        runtime = await self._ex._runtime_for(task.context_id, workspace)
        if is_new_context and self._ex._on_new_context is not None:
            await asyncio.to_thread(self._ex._on_new_context, task.context_id)
        self._runtime = runtime
        self._ex._aborts[task.id] = runtime
        runtime.set_a2a_task_id(task.id)

        # The consuming half of the one turn-event catalog: it owns the assistant-text
        # buffer and the turn telemetry span, translates every runtime event to its wire
        # part, and accumulates the turn's terminal text and stop reason.
        self._sink = _TurnEventSink(
            emit=self._emit,
            save_conversation=self._save_runtime_conversation,
            suspend=self._suspend_turn,
            telemetry_span=self._turn_span,
            model_identifier=lambda: self._runtime.effective_model_identifier if self._runtime is not None else "",
        )
        return _Prepared(resolved=resolved, runtime=runtime, sink=self._sink)

    async def _run_compaction_turn(self, prepared: _Prepared) -> object | None:
        """A manual compaction turn runs no model turn: it summarizes the older history
        in place and emits the compaction parts (the live indicator + the separator),
        then completes. Persisted like any turn, so the separator replays; fanned out, so
        viewers see it live. Returns ``_DONE`` when this was a compaction turn."""
        if not prepared.resolved.ingested.compaction:
            return None
        async for compaction_event in prepared.runtime.compact(reason="manual"):
            await prepared.sink.emit_compaction(compaction_event)
        await self._save_runtime_conversation()
        await self._updater.complete()
        return self._DONE

    async def _compose_turn_input(self, prepared: _Prepared) -> _ComposedTurn:
        """Build the model-facing input for this segment from the turn's payloads: an
        autonomous framing note, an artifact event, structured attachments (with vision
        blocks when the model supports them), or plain user text."""
        runtime = prepared.runtime
        # A render_error is injected as the model's own realization (a harness note
        # delivered in a <systemReminder> block), never as user prose; every other
        # artifact event is the turn's structured JSON input.
        self._as_system_note = self._autonomous
        if self._autonomous:
            # The wake message carries no prose (only an `autonomous_resume` part); the
            # framing note is supplied here and injected into the model as a
            # <systemReminder> harness note — cache-safe user-role delivery (see
            # AgentRuntime._harness_note_message) that never appears as user input in the
            # transcript.
            self._turn_input = _PROMPTS.load("background_resume_note", {})
        elif self._artifact_payload is not None:
            payload_json = json.dumps({"artifact_event": self._artifact_payload}, ensure_ascii=False)
            if self._artifact_payload.get("event") == "render_error":
                # The same JSON payload, wrapped in a self-realization note and injected as
                # a harness note rather than user input.
                self._turn_input = runtime.artifact_render_error_note(payload_json)
                self._as_system_note = True
            else:
                self._turn_input = payload_json
        elif self._structured_payloads:
            # The structured metadata (attachment file paths and any per-attachment
            # annotations) always rides along as a text block so the model can act on the
            # attachments with its tools. Images are inlined only when the agent model
            # advertises vision support; otherwise the model gets metadata/path access and
            # the UI receives a non-fatal warning. The model-facing copy of the data parts
            # keeps annotation positions on the normalized 0-999 grid documented in the
            # system prompt.
            text_payload = json.dumps({
                "text": self._user_text,
                "data_parts": normalize_annotation_payloads(self._structured_payloads),
            }, ensure_ascii=False)
            image_attachments = _image_attachments(self._structured_payloads)
            model_identifier = runtime.effective_model_identifier if runtime is not None else ""
            image_blocks = []
            if image_attachments and _model_supports_vision(model_identifier):
                image_blocks = [
                    block
                    for attachment in image_attachments
                    if (block := _image_content_block(attachment)) is not None
                ]
            elif image_attachments:
                await self._emit(_event_part(_attachment_warning_event(len(image_attachments), model_identifier)))
            if _model_supports_vision(model_identifier):
                # Artifact-image annotations: alongside the structured facts, a vision model
                # also gets the image itself with numbered circle badges stamped at each
                # annotation position (numbers match the payload's `sequence` values). Pure
                # Pillow work — run off-loop.
                image_blocks.extend(
                    await asyncio.to_thread(annotation_image_blocks, self._structured_payloads)
                )
            if image_blocks:
                self._turn_input = [{"type": "text", "text": text_payload}, *image_blocks]
                self._turn_has_images = True
            else:
                self._turn_input = text_payload
        else:
            self._turn_input = self._user_text
        runtime.set_pending_attachments(_all_attachments(self._structured_payloads))
        return _ComposedTurn(
            prepared=prepared,
            turn_input=self._turn_input,
            as_system_note=self._as_system_note,
        )

    async def _stream_and_finalize(self, composed: _ComposedTurn) -> None:
        """Drive the runtime's event stream through the sink, then close the task as
        completed or canceled once it drains (a durable suspension returns early)."""
        resolved = composed.prepared.resolved
        # A resume drives the turn from the durable checkpoint (the pending tool-call
        # AIMessage the rebuilt runtime already holds) with the answered decisions; a fresh
        # turn drives the model from this segment's input. Both feed the same event loop, so
        # a re-suspension is handled identically.
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
            # The user pressed Stop — end the task as canceled, not completed, so the
            # transcript and replay read it honestly as a stopped turn.
            await self._updater.cancel()
        else:
            await self._updater.complete()

    async def _fail(self, exception: Exception) -> None:
        await self._save_runtime_conversation()
        # Log the real exception server-side for debugging, but show the user only a safe
        # category — never the raw exception text.
        logger.exception("Agent turn failed: %s", exception)
        await self._updater.failed(self._updater.new_agent_message(
            [_event_part(ErrorEvent(**_safe_turn_error(exception, had_images=self._turn_has_images)))]
        ))

    async def _teardown(self) -> None:
        task = self._task
        with suppress(Exception):
            self._turn_span_context.__exit__(None, None, None)
        self._ex._aborts.pop(task.id, None)
        # The context's live state, or None if the session was deleted mid-turn
        # (teardown_context popped it). A torn-down context must not be re-persisted or
        # re-armed below — otherwise the aborted turn's teardown would resurrect the very
        # rows the delete flow just removed.
        state = self._ex._contexts.get(task.context_id)
        # Persist the conversation after the turn so a later restart can restore it. Only
        # save when there is something to save (a built runtime, or an already-cached
        # conversation) — an autonomous no-op wake has neither, and blindly saving an empty
        # list would clobber the persisted history. Skip entirely once the session is
        # deleted, so a stopped-and-deleted turn never re-orphans its row.
        if state is not None and (
            self._runtime is not None or task.context_id in self._ex._conversations
        ):
            messages = self._runtime.conversation if self._runtime is not None else self._ex._conversations.get(task.context_id, [])
            # Persist the agent's goal and task list atomically beside the end-of-turn
            # checkpoint when they changed this turn, so a restart restores the objective too
            # and the two can never drift apart. Clear the dirty flag only after the commit.
            session_state = self._runtime.dirty_session_snapshot() if self._runtime is not None else None
            await self._ex._task_store.save_turn_state(
                task.context_id, task.id, messages_to_dict(messages), session_state,
            )
            if session_state is not None and self._runtime is not None:
                self._runtime.clear_session_dirty()
        # Stop accepting steering for this context before draining the queue, then discard
        # anything that arrived too late to be honored (raced in after the loop's final
        # drain, or while the turn was ending/failing). Such messages were never applied to
        # the conversation; the client re-sends them as a fresh turn on stream close, so
        # leaving them queued here would double-apply them when the next turn drains the
        # runtime at its first boundary.
        if self._track_steerable_turn and state is not None:
            state.running = False
        if state is not None and state.runtime is not None:
            state.runtime.discard_pending_steering()
        if self._track_context_activity and self._on_turn_state is not None:
            self._on_turn_state(task.context_id, False)
        if self._context_serialization_lock is not None:
            self._context_serialization_lock.release()
        # If background work is still in flight after this turn, make sure a resume pump is
        # watching so the next completion wakes the session on its own. Pass the runtime this
        # turn used (not a cache lookup) so a reset that cleared the cache mid-turn cannot
        # strand the pending work.
        self._ex._arm_resume_pump(task.context_id, self._runtime)
