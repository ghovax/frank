"""One session's executor: the process-local half of a session that is alive.

A session is one worker process running one agent, so this holds exactly one runtime, one
turn lock, and one resume pump — where the monolith kept a dictionary of them keyed by
context. It builds the runtime on the session's first turn, keeps it warm across turns,
drives the autonomous wakes that deliver background results, and tears everything down when
the session is reaped.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import sys
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Optional

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers.request_handler import RequestHandler
from a2a.server.tasks import TaskUpdater
from a2a.types import DataPart, Message, MessageSendParams, Part, Role, Task, TaskState
from langchain_core.messages import messages_from_dict

from daisy.base.background_tasks import spawn_background_task
from daisy.base.configuration import GlobalConfiguration, load_agent_configuration
from daisy.base.background_store import get_background_job_store
from daisy.base.ports import JobStore
from daisy.base.workspaces import SessionWorkspace
from daisy.protocol.metadata import (
    AUTONOMOUS_RESUME_KIND,
    COMPACTION_KIND,
    REPORT_REMINDER_KIND,
    INPUT_RESPONSE_KIND,
    Metadata,
    envelope_part,
    turn_metadata_envelope,
)
from daisy.protocol.events import StatusEvent
from daisy.protocol.parts import _event_part
from daisy.protocol.turn_record import PendingInteraction, ToolGate, TurnRecord
from daisy.runtime.runtime import AgentRuntime
from daisy.runtime.turn_events import SuspensionGate
from daisy.worker.turn import _ContextState, _TurnRunner
from daisy.base.serialization import compact

logger = logging.getLogger(__name__)

class SessionExecutor(AgentExecutor):
    """The live half of one session.

    The turn machinery underneath still speaks in contexts, because that is the A2A shape and
    the resume/checkpoint logic is written against it; a worker simply only ever has one. The
    facade at the bottom of this class is what the socket server calls."""

    def __init__(
        self,
        *,
        session_id: str,
        agent_name: str,
        working_directory: str,
        permission_mode: str,
        global_configuration: GlobalConfiguration,
        sandbox: Optional[dict] = None,
        runtime_working_directory: str = "",
        project_id: str = "",
        parent: str = "",
        token: str = "",
        daemon_token: str = "",
        job_store: Optional[JobStore] = None,
    ):
        self._session_id = session_id
        self._agent_name = agent_name
        # A worker is a process a restart happens to, so its background jobs want the durable
        # store rather than the in-memory default a library session gets. Injectable all the
        # same: this is the layer that decides, not the runtime and not a module global.
        self._job_store: JobStore = job_store if job_store is not None else get_background_job_store()
        self._working_directory = working_directory
        # Where tools actually run. The daemon resolves this when the session is created —
        # a worktree workspace is not the project directory — and hands it over, so the
        # session never has to work it out mid-turn.
        self._runtime_working_directory = runtime_working_directory or working_directory
        self._permission_mode = permission_mode
        # Resolved and clamped by the daemon before this worker existed. Held as the profile the
        # tool layer applies to every child it spawns; the worker never widens it.
        from daisy.base.confinement import Profile

        self._sandbox = Profile.from_dict(sandbox)
        self._project_id = project_id
        self._parent = parent
        self._token = token
        self._global_configuration = global_configuration

        # The worker never opens the database. Every write goes to the daemon, which is the
        # sole writer, so the append-only store keeps the single-writer property it was built
        # around. Live turn events ride the same channel and reach whoever is attached.
        from daisy.base.paths import daemon_socket_path
        from daisy.worker.turn_store import DaemonTurnStore

        self._turn_store = DaemonTurnStore(str(daemon_socket_path()), session_id, daemon_token or token)

        # The same daemon, reached for a different purpose: composing with other sessions.
        # It carries this session's identity so every peer it creates is a child of this one —
        # not because a caller remembered to say so, but because there is no way to say
        # otherwise.
        from daisy.worker.peers import PeerSessions

        self._peers = PeerSessions(
            socket_path=str(daemon_socket_path()),
            # This session's own token, not the daemon's. The daemon token would work and say
            # nothing about who is calling; this one identifies the session, which is what
            # makes every peer it creates a child of this one and confines its reach to its
            # own subtree. Falling back to the daemon token keeps a session with no token of
            # its own (a bare worker in a test) able to compose at all.
            token=token or daemon_token,
            session_id=session_id,
            working_directory=runtime_working_directory or working_directory,
            permission_mode=permission_mode,
            parent_session=parent,
        )

        # A2A needs a handler to drive turns through; a worker serves exactly one session, so
        # it builds its own rather than being handed a registry of them.
        self._registry = None
        self._handler = None

        self._on_new_context = None
        self._session_permission_mode_for = None
        self._on_turn_state = self._notify_turn_state
        self._on_permission_state = self._notify_permission_state
        # Structured turn parts are handed to the daemon as they are persisted, so an attached
        # client sees the turn as it happens rather than on the next poll.
        self._on_stream_event = self._publish_stream_event
        self._file_lease_manager = None
        self._ensure_session_workspace = None
        self._ensure_mcp_servers = None
        self._resolve_locations = None

        self._contexts: dict[str, _ContextState] = {}
        # Maps an in-flight A2A task to its runtime, purely so `cancel` can abort it.
        self._aborts: dict[str, AgentRuntime] = {}
        # One session, one conversation. The monolith shared this map across agents so a
        # persona switch continued the same dialogue; a session is one agent for life now, so
        # the map has exactly one entry and exists only because the turn machinery indexes it.
        self._conversations: dict[str, list] = {}
        self._startup_resume_tasks: set[asyncio.Task] = set()
        self._compaction_tasks: set[asyncio.Task] = set()
        self._work_habits_acknowledged = False
        # A session is named after its first message, once.
        self._titled = False
        # The report reminder fires at most once for a session's whole life.
        self._nudged_to_report = False
        # This session's own MCP connections, and the task connecting them.
        self._mcp_manager = None
        self._mcp_connect: Optional[asyncio.Task] = None

    def _publish_stream_event(self, session_id: str, part) -> None:
        """Forward a turn part to the daemon for fan-out. Best effort and fire-and-forget: a
        watcher missing a frame is a cosmetic loss, and the turn must not wait on it."""
        payload = part.model_dump(by_alias=True, exclude_none=True, mode="json") if hasattr(part, "model_dump") else part
        spawn_background_task(self._turn_store.publish_event({"session_id": session_id, "part": payload}))

    def _notify_turn_state(self, session_id: str, running: bool) -> None:
        """Tell the daemon whether a turn is in flight, and whether anything still holds this
        process.

        `running` drives what `ps` and the sidebar show — working rather than merely alive.
        `retains` is what decides whether the worker may be put to sleep when the turn ends,
        and it has to be answered here because only this process knows: a background job is an
        in-process `asyncio.Task`, and sleeping the worker would kill it. Sent with the same
        event rather than asked for afterwards, so the daemon never has to round-trip to a
        session it is about to stop."""
        spawn_background_task(self._turn_store.publish_event({
            "session_id": session_id,
            "running": running,
            "retains": self._has_live_background_work(),
        }))

    def _has_live_background_work(self) -> bool:
        """Whether any runtime in this worker still has background work in flight.

        Deliberately includes results that finished but have not reached the model: sleeping
        then would drop the autonomous wake that was about to deliver them."""
        for context in self._contexts.values():
            runtime = getattr(context, "runtime", None)
            if runtime is None:
                continue
            if runtime.has_pending_jobs() or runtime.has_completed_undelivered_jobs():
                return True
        return False

    def _notify_permission_state(self, session_id: str, awaiting: bool) -> None:
        """Tell the daemon this session is parked on a human, so `ps` can show it as waiting
        rather than working."""
        spawn_background_task(
            self._turn_store.publish_event({"session_id": session_id, "awaiting_input": awaiting})
        )

    def compact_context(self, session_id: str) -> bool:
        """Trigger a manual compaction of a context's conversation (the user pressed
        the compact button). Runs as a background turn — serialized behind any active
        turn via the per-context lock — so the endpoint returns immediately while the
        compaction streams to the UI.

        A live runtime is not required: the compaction turn goes through the normal
        turn path, which rehydrates a persisted conversation into a fresh runtime, so
        a session reopened after a restart compacts without a warm-up message. The
        caller is responsible for routing to the owning agent's executor. Returns
        False only when there is no handler to drive the turn."""
        if self._agent_handler() is None:
            return False
        task = asyncio.create_task(self._run_compaction_turn(session_id))
        self._compaction_tasks.add(task)
        task.add_done_callback(self._compaction_tasks.discard)
        return True

    def _agent_handler(self) -> Optional[RequestHandler]:
        """The handler that drives this session's turns, built once on first use.

        A worker serves one session, so there is no registry to look it up in — it is simply
        this executor wrapped in the A2A request handler that knows how to stream a turn."""
        if self._handler is None:
            from a2a.server.request_handlers import DefaultRequestHandler

            # `task_store` is a2a's keyword, not ours — the object it takes is our turn store.
            self._handler = DefaultRequestHandler(agent_executor=self, task_store=self._turn_store)
        return self._handler

    async def _drive_self_sent_turn(
        self, session_id: str, envelope_kind: str, *, metadata_flags: dict,
    ) -> None:
        """Drive one harness-initiated turn through the ordinary turn path via a self-sent
        agent-role message carrying only a prose-less envelope part, so it is a real,
        persisted, replayable task streamed to viewers like any other turn. The single shared
        driver behind the compaction and autonomous-wake turns — same shape, one place."""
        handler = self._agent_handler()
        if handler is None:
            return
        message = Message(
            role=Role.agent,
            parts=[envelope_part(envelope_kind)],
            message_id=uuid.uuid4().hex,
            context_id=session_id,
            metadata=turn_metadata_envelope(metadata_flags),
        )
        async for _event in handler.on_message_send_stream(MessageSendParams(message=message)):
            pass

    async def nudge_to_report(self, session_id: str) -> None:
        """Drive one turn whose only purpose is to say "you have not answered yet".

        Fired once per session, when a turn finishes and the session that created this one has
        still heard nothing. Reporting back cannot be enforced — only the model can decide what
        its answer is — so this is the one reminder, and the daemon's notice when the session is
        finally reaped remains the backstop. Nudging on every turn would be worse than not
        nudging at all: a peer legitimately mid-way through a long job would be told it is late
        over and over."""
        if self._nudged_to_report:
            return
        self._nudged_to_report = True
        await self._drive_self_sent_turn(
            session_id, REPORT_REMINDER_KIND, metadata_flags={Metadata.REPORT_REMINDER: True},
        )

    async def _run_compaction_turn(self, session_id: str) -> None:
        """Drive one manual-compaction turn (a self-sent agent-role compaction message), so
        it is a real, persisted, replayable task streamed to viewers like any other turn."""
        await self._drive_self_sent_turn(session_id, COMPACTION_KIND, metadata_flags={Metadata.COMPACTION: True})

    def abort_context(self, session_id: str) -> bool:
        # Stop is broadcast to every executor (chat.py), but only the one that actually
        # holds this context's state has anything to stop.
        state = self._contexts.get(session_id)
        if state is None or (state.runtime is None and state.resume_pump is None):
            return False
        # Mark Stopped so the turn's finally (and any later completion) cannot re-arm a
        # resume pump, then cancel the pump already watching it — the autonomous wake is
        # what otherwise revived a fresh, abort-cleared turn seconds after Stop.
        state.aborted = True
        handled = False
        if state.resume_pump is not None:
            if not state.resume_pump.done():
                state.resume_pump.cancel()
            state.resume_pump = None
            handled = True
        if state.runtime is not None:
            state.runtime.abort()
            handled = True
        return handled

    def abort_tool(self, session_id: str, tool_call_identifier: str) -> bool:
        state = self._contexts.get(session_id)
        if state is None or state.runtime is None:
            return False
        return state.runtime.abort_tool(tool_call_identifier)


    def send_tool_to_background(self, session_id: str, tool_call_identifier: str) -> bool:
        state = self._contexts.get(session_id)
        if state is None or state.runtime is None:
            return False
        return state.runtime.send_tool_to_background(tool_call_identifier)

    def background_snapshots(self, session_id: str) -> list[dict]:
        state = self._contexts.get(session_id)
        if state is None or state.runtime is None:
            return []
        return state.runtime.background_snapshots()

    def reset_runtimes(self) -> None:
        """Drop cached runtimes so the next turn rebuilds them — used after a
        configuration change (e.g. new API credentials, an mcp.json edit) so it takes
        effect without a restart. Eviction is deferred for any context that still has
        background work in flight, so a reset in one session never strands another
        session's in-flight result: the resume pump keys off the live runtime, and the
        durable store is only replayed on startup / the next user turn, so dropping the
        runtime mid-flight would silently hang that session. In-flight turns keep their
        own runtime reference and are unaffected."""
        for session_id, state in list(self._contexts.items()):
            if state.runtime is not None:
                state.pending_reset = True
                self._maybe_evict(session_id)

    async def _claim_work_habits_acknowledgement(self, session_id: str, **_flags) -> bool:
        """Whether this turn should emit the once-per-session work-habits acknowledgement.

        Claimed through the daemon rather than remembered here, and that is not fussiness: a
        worker is created fresh per *activation* now, not per session, so a session that slept
        between turns and woke again would show the acknowledgement every time it woke. The
        in-memory flag is kept in front of it purely to save a round trip within one activation.
        """
        if not self._global_configuration.user_context.enabled:
            return False
        if self._work_habits_acknowledged:
            return False
        claimed = await self._turn_store.claim_work_habits(session_id)
        self._work_habits_acknowledged = True
        return claimed


    def _record_pending_answer(self, task, payload: dict) -> Optional[tuple[dict, dict]]:
        """Record one human answer into the task's durable pending-interaction record and
        report whether the batch is now fully answered. Returns ``(plans, answers)`` when
        every gate has an answer (ready to resume), else ``None`` (a partial answer, or an
        answer for a request this task is not waiting on). Mutates ``task.metadata`` in
        place; the caller persists it. This is the single resolution path an external
        ``input_response`` message and the native REST resolve both feed."""
        record = TurnRecord.from_metadata(task.metadata)
        pending = record.pending
        if pending is None:
            return None
        request_id = str(payload.get("request_id", ""))
        gate = pending.gate_for(request_id)
        if gate is None:
            return None
        if gate.is_question:
            pending.answers[request_id] = {"__declined__": True} if payload.get("declined") else payload.get("answers", [])
        else:
            pending.answers[request_id] = str(payload.get("decision", "deny"))
        task.metadata = record.apply_to(task.metadata)
        if pending.fully_answered:
            return pending.plans, pending.answers
        return None


    def steer_context(self, session_id: str, message: str) -> bool:
        state = self._contexts.get(session_id)
        if state is None or not state.running or state.runtime is None:
            return False
        return state.runtime.enqueue_steering(message)

    def reset_runtime(self, session_id: str) -> None:
        """Drop a single context's cached runtime so the next turn rebuilds it —
        used when the session's model override changes, so the new model takes
        effect without a server restart. Deferred while the context still has
        background work in flight, so evicting the runtime never strands a completed
        result the resume pump still needs to deliver (which would hang the session)."""
        state = self._contexts.get(session_id)
        if state is None:
            return
        state.pending_reset = True
        self._maybe_evict(session_id)

    def _maybe_evict(self, session_id: str) -> None:
        """Apply a deferred runtime reset once the runtime is idle. A reset requested
        while the context still had background work in flight is held back (the live
        resume pump must keep delivering results, since the durable store is only
        replayed on startup / the next user turn). Dropping the runtime here, only
        after its jobs have drained, lets the next turn rebuild with the new
        configuration without ever orphaning an undelivered result. The state (and its
        lock) survives — only the runtime slot is cleared."""
        state = self._contexts.get(session_id)
        if state is None or not state.pending_reset:
            return
        if state.runtime is not None and state.runtime.has_pending_jobs():
            return  # still delivering — keep the runtime (and its pump) alive
        state.runtime = None
        state.pending_reset = False

    def _context(self, session_id: str) -> _ContextState:
        """The per-context state, created on first access. Use this when a turn is about
        to establish state (acquire the lock, cache a runtime, set a flag); use
        ``self._contexts.get`` when merely inspecting a context that may not exist."""
        state = self._contexts.get(session_id)
        if state is None:
            state = _ContextState()
            self._contexts[session_id] = state
        return state

    def teardown_context(self, session_id: str) -> None:
        """Release every trace of a deleted session: cancel its resume pump, abort a
        live runtime, drop its per-context state, and forget its shared conversation.
        The single point where a session's live state is freed, so deleting sessions can
        never accumulate orphaned runtimes, pumps, locks, or history. Idempotent and safe
        to broadcast to every executor (only the owner holds any state)."""
        state = self._contexts.pop(session_id, None)
        if state is not None:
            if state.resume_pump is not None and not state.resume_pump.done():
                state.resume_pump.cancel()
            if state.runtime is not None:
                state.runtime.abort()
        self._conversations.pop(session_id, None)

    def _arm_resume_pump(self, session_id: str, runtime: Optional[AgentRuntime] = None) -> None:
        """Ensure a resume pump watches this context while it has background work in
        flight. Idempotent — a live pump is left running. ``runtime`` is the runtime
        the just-finished turn actually used; it is passed explicitly rather than
        re-read from the state so a concurrent reset that cleared the cached runtime
        cannot strand the pending work between the turn ending and the pump arming. If
        that runtime still has pending jobs but the state's runtime slot is empty (a
        reset dropped it), restore it — marked for a deferred reset — so both the pump
        and the autonomous delivery drain the very same ``BackgroundJobs``.

        Only ever arms for a context that still has live state: a torn-down (deleted)
        session has none, so a late call from an ending turn's finally is a clean no-op
        that cannot resurrect a deleted session's pump."""
        state = self._contexts.get(session_id)
        # A Stopped or torn-down context stays quiet: don't arm a pump that would
        # autonomously wake the agent. Pending work is replayed on the next user turn.
        if state is None or state.aborted:
            return
        runtime = runtime or state.runtime
        if runtime is None or not runtime.has_pending_jobs():
            return
        if state.runtime is None:
            state.runtime = runtime
            state.pending_reset = True
        if state.resume_pump is not None and not state.resume_pump.done():
            return
        state.resume_pump = asyncio.create_task(self._resume_pump(session_id))

    async def _resume_pump(self, session_id: str) -> None:
        """Wait (event-driven, at zero cost) for each background result to land while
        the context is otherwise idle, driving an autonomous turn to deliver each. It
        loops until nothing is pending, then retires."""
        try:
            while True:
                state = self._contexts.get(session_id)
                runtime = state.runtime if state is not None else None
                if runtime is None or not runtime.has_pending_jobs():
                    return
                await runtime.wait_for_jobs()
                # A result landed — drive an autonomous turn to deliver it. If a
                # concurrent user turn already drained it, that turn no-ops.
                await self._run_autonomous_turn(session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background-resume pump failed for context %s", session_id)
        finally:
            # Clear the slot only if it still points at *this* pump — a freshly armed
            # pump (the user resumed) must not be dropped by a late-finishing finally.
            state = self._contexts.get(session_id)
            if state is not None and state.resume_pump is asyncio.current_task():
                state.resume_pump = None
            # The context is idle now, so any reset deferred while it had work in
            # flight can finally take effect (rebuilding with the new configuration).
            self._maybe_evict(session_id)

    async def _run_autonomous_turn(self, session_id: str) -> None:
        """Start a turn the user did not initiate, to deliver a completed background
        result. It reuses the ordinary turn path via a self-sent A2A message, so the
        wake is a real, persisted, replayable task streamed to viewers like any other
        turn — the agent genuinely picks the work back up on its own."""
        if self._agent_handler() is None:
            return
        # Nothing left to deliver — a concurrent user turn already drained the result
        # while the pump was scheduling this wake — so don't even mint a task. The
        # executor re-checks this under the per-context lock (that's the authoritative
        # guard against the race); short-circuiting here just avoids creating an empty,
        # invisible wake task in the common case.
        state = self._contexts.get(session_id)
        runtime = state.runtime if state is not None else None
        has_live_result = runtime is not None and runtime.has_completed_undelivered_jobs()
        has_stored_result = self._job_store.has_undelivered_jobs(session_id, self._agent_name)
        if not has_live_result and not has_stored_result:
            return
        # An agent-authored message (the agent resumed itself), carrying only the prose-less
        # `autonomous_resume` part. Modelling it as `agent` rather than `user` keeps the
        # persisted A2A task honest — no consumer sees a user message the user never sent —
        # and the part renders as nothing. The framing note reaches only the model, injected
        # as a system note in `execute`. Driven through the shared self-sent-turn path.
        await self._drive_self_sent_turn(session_id, AUTONOMOUS_RESUME_KIND, metadata_flags={Metadata.AUTONOMOUS_RESUME: True})

    def _build_runtime(
        self,
        session_id: str,
        working_directory: str,
        project_directory: str,
        conversation: Optional[list] = None,
        locations: Optional[list[dict]] = None,
    ) -> AgentRuntime:
        configuration = load_agent_configuration(
            self._agent_name, self._global_configuration.agent_directories_for(project_directory)
        )
        runtime = AgentRuntime(
            agent_configuration=configuration,
            global_configuration=self._global_configuration,
            session_id=session_id,
            conversation=conversation,
            working_directory=working_directory or "",
            project_directory=project_directory or working_directory or "",
            file_lease_manager=self._file_lease_manager,
            locations=locations,
            # Who to answer. A session was already told another one might be waiting on its
            # result and given no way to find out which; the return path is a message, and a
            # message needs an address.
            parent_session=self._parent,
            sandbox=self._sandbox,
            # The two things the runtime cannot derive from configuration: how this session
            # reaches its peers, and the MCP connections this worker owns.
            session_access=self._peers,
            mcp_manager=self._mcp_manager,
        )
        stream_event_callback = self._on_stream_event
        if stream_event_callback is not None:
            runtime.set_agent_event_sink(lambda event: stream_event_callback(
                session_id,
                Part(root=DataPart(data=event)),
            ))
        runtime.set_turn_reader(self._make_turn_reader())
        return runtime

    def _make_turn_reader(self):
        async def read_turn(turn_id: str):
            if not turn_id:
                return None
            task = await self._turn_store.get(turn_id)
            # Exclude `history` for the same reason as the delegate's done payload:
            # read_turn feeds a sibling/agent task straight into the caller's
            # model context, and the full transcript (incl. raw web-search text)
            # would overflow it. Only the status + deliverable are needed.
            return task.model_dump(by_alias=True, exclude_none=True, mode="json", exclude={"history"}) if task else None
        return read_turn

    async def _runtime_for(self, session_id: str, workspace: SessionWorkspace) -> AgentRuntime:
        # Apply any reset that was deferred while this context had background work in
        # flight: if the runtime has since gone idle, drop it now so this turn rebuilds
        # it with the new configuration rather than reusing the stale one.
        self._maybe_evict(session_id)
        state = self._context(session_id)
        runtime = state.runtime
        if runtime is None:
            # Restore a persisted conversation the first time a context is seen this
            # process (e.g. a session reopened after a restart), so the agent resumes
            # with the same history the UI is replaying rather than a blank slate.
            if session_id not in self._conversations:
                # The model-facing conversation is the task store's per-context checkpoint
                # (the single durable turn surface); a reopened session resumes from it.
                restored = messages_from_dict(await self._turn_store.load_checkpoint(session_id))
                if restored:
                    self._conversations[session_id] = restored
            # Seed from (and bind to) the process-wide dialogue history for this
            # context — the same list object another agent may have been writing
            # to — so a persona switch picks up exactly where the last turn left off.
            conversation = self._conversations.setdefault(session_id, [])
            locations = None
            if self._resolve_locations is not None:
                locations = await asyncio.to_thread(self._resolve_locations, session_id) or None
            runtime = self._build_runtime(
                session_id,
                workspace.runtime_working_directory,
                workspace.source_working_directory,
                conversation=conversation,
                locations=locations,
            )
            if self._session_permission_mode_for is not None:
                runtime.set_permission_mode(await asyncio.to_thread(self._session_permission_mode_for, session_id))
            # Restore the agent's durable objective — its goal and task list — the first
            # time this process builds a runtime for the context (e.g. a session reopened
            # after a restart), alongside the conversation restored above, so a marathon run
            # never loses what it was working toward.
            session_state = await self._turn_store.load_session_state(session_id)
            if session_state:
                runtime.restore_session(session_state)
            state.runtime = runtime
            # First time this process builds a runtime for the context: replay any
            # background results the durable store holds but never delivered (e.g.
            # a parse that finished, or was interrupted, across a restart), so the
            # model sees them as soon as this — or the autonomous wake — runs.
            self._replay_stored_background_results(session_id, runtime)
        # A context's working directory is fixed at creation — a session stays
        # bound to the folder it was started in, so later turns never repoint it.
        return runtime

    def _replay_stored_background_results(self, session_id: str, runtime: AgentRuntime) -> None:
        store = self._job_store
        for job in store.undelivered_jobs(session_id, self._agent_name):
            runtime.inject_stored_background_result(
                kind=job["kind"],
                identifier=job["job_id"],
                tool_call_identifier=job["tool_call_id"],
                result=job["result"] or "",
            )
            store.mark_delivered(job["job_id"])

    async def resume_pending_on_startup(self) -> None:
        """After a restart, recover this agent's persisted background work. A job that
        was still running cannot be resurrected as a live coroutine, so it is recorded
        as interrupted (the model is told to re-run it if the result is still needed);
        then every context that now has a deliverable result is woken autonomously."""
        store = self._job_store
        for job in store.running_jobs(self._agent_name):
            store.mark_abandoned(job["job_id"], compact({
                "code": f"{job['kind']}_interrupted",
                "job_id": job["job_id"],
                "message": (
                    "This task was interrupted by a server restart before it finished. "
                    "Re-run it if the result is still needed."
                ),
                "arguments": job["arguments"],
            }))
        for session_id in store.contexts_with_undelivered(self._agent_name):
            wake_task = asyncio.create_task(self._run_autonomous_turn(session_id))
            self._startup_resume_tasks.add(wake_task)
            wake_task.add_done_callback(self._startup_resume_tasks.discard)

    def _workspace(self, requested_working_directory: str = "") -> SessionWorkspace:
        """Where this session's work happens.

        The daemon resolved this when the session was created — a worktree strategy puts the
        tools somewhere other than the project directory — and handed it over in the
        assignment. It is not renegotiated per turn: where a session runs is part of what the
        session *is*, fixed at creation alongside its agent and its permission mode."""
        source = requested_working_directory or self._working_directory or ""
        runtime = self._runtime_working_directory or source
        return SessionWorkspace(
            source_working_directory=source,
            runtime_working_directory=runtime,
            strategy="worktree" if runtime and runtime != source else "none",
        )

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Run one turn. The whole per-turn state machine — ingest, resolve, build the
        runtime, stream, finalize, tear down — lives in :class:`_TurnRunner`; each request
        gets a fresh single-use instance."""
        await _TurnRunner(self, context, event_queue).run()

    async def _suspend_durable_segment(
        self,
        task: Task,
        updater: TaskUpdater,
        interactions: list[SuspensionGate],
        plans: dict,
        save_conversation: Callable[[], Awaitable[None]],
    ) -> bool:
        """Close a top-level turn's A2A segment as a durable suspend.

        Records the pending interactions and the runtime checkpoint into task
        metadata, flags the session as awaiting input, persists both the
        conversation and the task, then reports ``input_required`` (final) so a
        later answer rebuilds from the checkpoint and resumes. Returns ``True`` to
        signal the stream loop to stop consuming this segment.
        """
        suspended = TurnRecord.from_metadata(task.metadata)
        suspended.pending = PendingInteraction(
            gates=[ToolGate.model_validate(dataclasses.asdict(gate)) for gate in interactions],
            plans=plans,
            agent=self._agent_name,
        )
        task.metadata = suspended.apply_to(task.metadata)
        if self._on_permission_state is not None:
            self._on_permission_state(task.context_id, True)
        await save_conversation()
        await self._turn_store.save(task)
        await updater.update_status(
            TaskState.input_required,
            updater.new_agent_message([_event_part(StatusEvent(code="input_required"))]),
            final=True,
        )
        return True

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # `context.task_id` is a2a's attribute, not ours to rename.
        turn_id = context.task_id or (context.current_task.id if context.current_task else "")
        runtime = self._aborts.get(turn_id)
        if runtime is not None:
            runtime.abort()
        if context.current_task:
            updater = TaskUpdater(event_queue, context.current_task.id, context.current_task.context_id)
            await updater.cancel()

    # The facade the session's socket serves.
    #
    # Everything above is the turn machinery, which still speaks in contexts because that is
    # the A2A shape. A worker only ever has one, so these methods bind that machinery to this
    # session and are the whole surface the socket exposes.

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def token(self) -> str:
        return self._token

    @property
    def is_running(self) -> bool:
        state = self._contexts.get(self._session_id)
        return bool(state is not None and state.running)

    async def start(self) -> None:
        """Prepare the session before its socket opens.

        Deliberately does not build the runtime: that costs a model client and an MCP connect,
        and a session created but never messaged should pay for neither.

        What happens here is the two installations that are genuinely process-wide — telemetry,
        which exports for the whole process, and the tuning policy, which every tool reads — plus
        this session's own MCP connections, whose lifetime the worker owns. Everything else the
        tools need travels with the runtime in its tool context, so a settings change cannot be
        half-applied and two turns cannot see each other's confinement.
        """
        from daisy.base.telemetry import configure as configure_telemetry
        from daisy.base.tuning import set_tuning, tuning_from_policy

        configuration = self._global_configuration
        set_tuning(tuning_from_policy(configuration.tuning))

        telemetry = configuration.telemetry
        configure_telemetry(
            enabled=telemetry.enabled,
            endpoint=telemetry.exporter.endpoint,
            headers=telemetry.resolved_headers(),
            sample_ratio=telemetry.sample_ratio,
        )

        # Each session connects its own MCP servers. The daemon keeps a pool of its own for the
        # browser's server list, but connections are stateful and a session's tool calls belong
        # to the session's process — sharing one pool across processes is not something a stdio
        # server can offer. The manager is handed to each runtime this session builds; the
        # worker keeps it because the worker is what closes it.
        servers = configuration.mcp.enabled_servers()
        if servers:
            from daisy.base.mcp_client import MCPClientManager

            self._mcp_manager = MCPClientManager(servers)
            # Connected in the background: a hung server must not delay the session's socket,
            # and tool gating keys on the manager existing rather than on live connections.
            self._mcp_connect = asyncio.create_task(self._mcp_manager.start())

        self._context(self._session_id)

    async def start_turn(self, parts: list, metadata: dict) -> str:
        """Drive a turn from an inbound message, returning its task id."""
        handler = self._agent_handler()
        if handler is None:
            raise RuntimeError("This session has no request handler.")
        self._title_from_first_message(parts)
        message = Message(
            role=Role.user,
            parts=parts,
            message_id=uuid.uuid4().hex,
            context_id=self._session_id,
            metadata=turn_metadata_envelope(metadata) if metadata else None,
        )
        turn_id = ""
        async for event in handler.on_message_send_stream(MessageSendParams(message=message)):
            if isinstance(event, Task) and not turn_id:
                turn_id = event.id
        return turn_id

    def _title_from_first_message(self, parts: list) -> None:
        """Name the session after what it was first asked to do.

        Generated here rather than in the daemon because it means calling a model, and the
        model machinery lives in the session's process. Fired once, in the background, so a
        title never delays the turn it describes."""
        if self._titled:
            return
        self._titled = True
        prose = " ".join(
            str(getattr(getattr(part, "root", part), "text", "") or "") for part in parts
        ).strip()
        if not prose:
            return
        spawn_background_task(self._generate_title(prose))

    async def _generate_title(self, first_message: str) -> None:
        """Ask the configured model for a short title, and hand it to the daemon.

        The schema is bound as a tool with automatic choice rather than through structured
        output: reasoning models reject both a json_schema response format and the forced tool
        choice structured output relies on, but take an ordinary tool call — the same shape the
        agent's own turns use. Reasoning is dialled down because a title is trivial and must
        stay cheap; the agent's configured effort is for the work, not for naming it.

        Silent on every failure: an unnamed session is a cosmetic loss, and a session must
        never fail because it could not think of a title for itself."""
        from langchain_core.messages import HumanMessage, SystemMessage

        from daisy.base.configuration import PromptLoader
        from daisy.protocol.dtos import SessionTitle
        from daisy.runtime.internals import model_is_authorized
        from daisy.runtime.runtime import build_chat_model

        try:
            configuration = load_agent_configuration(
                self._agent_name,
                self._global_configuration.agent_directories_for(self._working_directory),
            )
            model_identifier = configuration.model_identifier
            if not model_identifier or not model_is_authorized(model_identifier, self._global_configuration):
                return
            titling_configuration = configuration.model_copy(update={"reasoning_effort": "low"})
            model = build_chat_model(
                model_identifier, self._global_configuration, titling_configuration
            ).bind_tools([SessionTitle], tool_choice="auto")
            prompt = PromptLoader(Path(__file__).resolve().parent.parent / "runtime" / "prompts")
            response = await model.ainvoke([
                SystemMessage(content=prompt.load("session_title", {})),
                HumanMessage(content=first_message),
            ])
            if not response.tool_calls:
                return
            title = (SessionTitle.model_validate(response.tool_calls[0]["args"]).title or "").strip()
            if title:
                await self._turn_store.publish_title(title)
        except Exception:  # noqa: BLE001 — a session is not worth failing over its own name
            logger.debug("Could not generate a title for session %s", self._session_id, exc_info=True)

    def inject(self, text: str) -> bool:
        """Deliver a message into the turn that is already running, at its next safe point.

        This is what makes a peer's question reach a session that is mid-tool rather than
        waiting for it to finish. Returns False when the turn ended first, so the caller can
        fall back to starting a fresh turn instead of dropping the message."""
        state = self._contexts.get(self._session_id)
        if state is None or not state.running or state.runtime is None:
            return False
        return state.runtime.enqueue_steering(text)

    async def resolve_pending_input(self, payload: dict) -> bool:
        """Record a human's answer to a parked gate and resume the turn once every gate in the
        batch has one."""
        handler = self._agent_handler()
        if handler is None:
            return False
        tasks = await self._turn_store.turns_for_session(self._session_id)
        for task in tasks:
            pending = TurnRecord.from_metadata(task.metadata).pending
            if pending is None or pending.gate_for(str(payload.get("request_id", ""))) is None:
                continue
            message = Message(
                role=Role.user,
                parts=[envelope_part(INPUT_RESPONSE_KIND, **payload)],
                message_id=uuid.uuid4().hex,
                task_id=task.id,
                context_id=self._session_id,
            )
            spawn_background_task(self._drive_input_response(handler, message))
            return True
        return False

    async def _drive_input_response(self, handler, message: Message) -> None:
        try:
            async for _event in handler.on_message_send_stream(MessageSendParams(message=message)):
                pass
        except Exception:  # noqa: BLE001 — a failed resume must not take the session down
            logger.exception("Resuming session %s after an answer failed", self._session_id)

    def abort(self) -> bool:
        return self.abort_context(self._session_id)

    def abort_tool_call(self, tool_call_identifier: str) -> bool:
        return self.abort_tool(self._session_id, tool_call_identifier)

    async def abort_pending_input(self) -> bool:
        """Deny every gate this session is parked on.

        The last denial resumes the turn, which records a denial for each blocked call — that
        is what keeps the conversation valid, since a tool call with no result is a message the
        next turn cannot build on. Returns whether anything was actually pending."""
        tasks = await self._turn_store.turns_for_session(self._session_id)
        for task in tasks:
            pending = TurnRecord.from_metadata(task.metadata).pending
            if pending is None or not pending.gates:
                continue
            for gate in pending.gates:
                payload = (
                    {"request_id": gate.request_id, "declined": True}
                    if gate.is_question
                    else {"request_id": gate.request_id, "decision": "deny"}
                )
                await self.resolve_pending_input(payload)
            return True
        return False

    def compact(self) -> bool:
        """Compact this session's conversation as a background turn."""
        return self.compact_context(self._session_id)

    def background_tool_call(self, tool_call_identifier: str) -> bool:
        """Detach a still-blocking foreground command so the turn can continue."""
        return self.send_tool_to_background(self._session_id, tool_call_identifier)

    def background_jobs(self) -> list[dict]:
        """The background work this session currently has in flight."""
        return self.background_snapshots(self._session_id)

    def card_payload(self) -> dict:
        """What this session advertises at its well-known path.

        Degrades to a minimal card rather than failing: discovery is the first thing a peer
        does, and answering it with a 500 hides whatever the real problem was behind a
        transport error."""
        try:
            return self._build_card_payload()
        except Exception:  # noqa: BLE001 — a card is descriptive, never load-bearing
            logger.exception("Building the agent card for session %s failed", self._session_id)
            return {
                "name": self._agent_name,
                "description": f"Daisy session {self._session_id}.",
                "version": "1.0.0",
                "protocolVersion": "0.3.0",
                "url": f"unix:{self._session_id}",
                "defaultInputModes": ["text/plain"],
                "defaultOutputModes": ["text/plain"],
                "capabilities": {"streaming": True},
                "skills": [],
            }

    def _build_card_payload(self) -> dict:
        from daisy.base.skills import load_skills, skills_for_agent
        from daisy.protocol.card import build_agent_card

        configuration = load_agent_configuration(
            self._agent_name, self._global_configuration.agent_directories_for(self._working_directory)
        )
        skills = skills_for_agent(
            load_skills(self._global_configuration.skill_directories_for(self._working_directory)),
            configuration.skills,
        )
        card = build_agent_card(configuration, skills, f"unix:{self._session_id}")
        return card.model_dump(by_alias=True, exclude_none=True, mode="json")

    def status_payload(self) -> dict:
        state = self._contexts.get(self._session_id)
        return {
            "session_id": self._session_id,
            "agent": self._agent_name,
            "running": bool(state is not None and state.running),
            "permission_mode": self._permission_mode,
        }

    async def aclose(self) -> None:
        """Stop cleanly: abort any turn in flight so its conversation is checkpointed, cancel
        the background work it started, close this session's MCP connections, and release the
        store. Ordered so nothing is torn down while something else still needs it, and each
        step guarded so one failure cannot strand the rest."""
        import contextlib

        from daisy.runtime.background import cancel_all_background_jobs

        self.teardown_context(self._session_id)
        with contextlib.suppress(Exception):
            cancel_all_background_jobs()
        # The browser surface holds a connection to the user's Chrome if a screen tool ever
        # ran. Closed here, by the session that opened it, rather than from an `atexit` hook
        # the module used to register on import — imported lazily, so a session that never
        # touched the screen does not load PyObjC just to shut down.
        if "daisy.computer.web" in sys.modules:
            with contextlib.suppress(Exception):
                sys.modules["daisy.computer.web"].close()
        if self._mcp_connect is not None and not self._mcp_connect.done():
            self._mcp_connect.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._mcp_connect
        if self._mcp_manager is not None:
            with contextlib.suppress(Exception):
                await self._mcp_manager.aclose()
        close = getattr(self._turn_store, "aclose", None)
        if close is not None:
            await close()
