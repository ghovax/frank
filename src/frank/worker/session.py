"""One session's executor: the process-local half of a session that is alive."""

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
from a2a.types import Message, MessageSendParams, Role, Task, TaskState
from langchain_core.messages import messages_from_dict

from frank.base.background_tasks import spawn_background_task
from frank.base.catalogue import machine_catalogue
from frank.base.tuning import Tunable, active_tuning
from frank.base.file_leases import FileLeaseManager
from frank.base.configuration import Configuration
from frank.base.background_store import get_background_job_store
from frank.base.ports import JobStore
from frank.base.worktrees import SessionWorktree
from frank.protocol.metadata import (
    AUTONOMOUS_RESUME_KIND,
    COMPACTION_KIND,
    GOAL_CONTINUATION_KIND,
    REPORT_REMINDER_KIND,
    INPUT_RESPONSE_KIND,
    Metadata,
    envelope_part,
    turn_metadata_envelope,
)
from frank.protocol.events import StatusEvent
from frank.protocol.parts import _event_part
from frank.protocol.turn_record import PendingInteraction, ToolGate, TurnRecord
from frank.runtime.runtime import AgentRuntime
from frank.runtime.turn_events import SuspensionGate
from frank.worker.turn import _ContextState, _TurnRunner
from frank.base.serialization import compact

logger = logging.getLogger(__name__)

class SessionExecutor(AgentExecutor):
    """The live half of one session."""

    def __init__(
        self,
        *,
        session_id: str,
        agent_name: str,
        working_directory: str,
        permission_mode: str,
        global_configuration: Configuration,
        sandbox: Optional[dict] = None,
        runtime_working_directory: str = "",
        workspace_id: str = "",
        locations: Optional[list[dict]] = None,
        parent: str = "",
        token: str = "",
        daemon_token: str = "",
        job_store: Optional[JobStore] = None,
    ):
        self._session_id = session_id
        self._agent_name = agent_name
        # A worker is a process a restart happens to, so its background jobs want the durable store rather than the in-memory default a library session gets.
        self._job_store: JobStore = job_store if job_store is not None else get_background_job_store()
        self._working_directory = working_directory
        # Where tools actually run.
        self._runtime_working_directory = runtime_working_directory or working_directory
        self._permission_mode = permission_mode
        # Resolved and clamped by the daemon before this worker existed.
        from frank.base.confinement import Profile

        self._sandbox = Profile.from_dict(sandbox)
        self._workspace_id = workspace_id
        self._parent = parent
        self._token = token
        self._global_configuration = global_configuration

        # The worker never opens the database.
        from frank.base.paths import daemon_socket_path
        from frank.worker.turn_store import DaemonTurnStore

        self._turn_store = DaemonTurnStore(str(daemon_socket_path()), session_id, daemon_token or token)

        # The same daemon, reached for a different purpose: composing with other sessions.
        from frank.worker.peers import PeerSessions

        self._peers = PeerSessions(
            socket_path=str(daemon_socket_path()),
            # This session's own token, not the daemon's.
            token=token or daemon_token,
            session_id=session_id,
            working_directory=runtime_working_directory or working_directory,
            permission_mode=permission_mode,
            parent_session=parent,
        )

        # A2A needs a handler to drive turns through; a worker serves exactly one session, so it builds its own rather than being handed a registry of them.
        self._registry = None
        self._handler = None

        # Where this session's tools may run, resolved by the daemon at `session.create` and carried in the assignment — the same once-and-immutable treatment the sandbox and the permission mode get, and for the same reason.
        self._locations = locations
        self._on_turn_state = self._notify_turn_state
        self._on_permission_state = self._notify_permission_state
        # Structured turn parts are handed to the daemon as they are persisted, so an attached client sees the turn as it happens rather than on the next poll.
        self._on_stream_event = self._publish_stream_event
        # Advisory locks so two sessions editing the same file notice each other.
        self._file_lease_manager = FileLeaseManager()

        self._contexts: dict[str, _ContextState] = {}
        # Maps an in-flight A2A task to its runtime, purely so `cancel` can abort it.
        self._aborts: dict[str, AgentRuntime] = {}
        # One session, one conversation.
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
        # Held so the screen warm-up is not collected mid-flight; nothing ever awaits it.
        self._screen_warm: Optional[asyncio.Task] = None

    def _publish_stream_event(self, session_id: str, part) -> None:
        """Forward a turn part to the daemon for fan-out."""
        payload = part.model_dump(by_alias=True, exclude_none=True, mode="json") if hasattr(part, "model_dump") else part
        spawn_background_task(self._turn_store.publish_event({"session_id": session_id, "part": payload}))

    def _notify_turn_state(self, session_id: str, running: bool) -> None:
        """Tell the daemon whether a turn is in flight, and whether anything still holds this process."""
        spawn_background_task(self._turn_store.publish_event({
            "session_id": session_id,
            "running": running,
            "retains": self._has_live_background_work(),
        }))

    def _has_live_background_work(self) -> bool:
        """Whether any runtime in this worker still has background work in flight."""
        for context in self._contexts.values():
            runtime = getattr(context, "runtime", None)
            if runtime is None:
                continue
            if runtime.has_pending_jobs() or runtime.has_completed_undelivered_jobs():
                return True
        return False

    def _notify_permission_state(self, session_id: str, awaiting: bool) -> None:
        """Tell the daemon this session is parked on a human, so `ps` can show it as waiting rather than working."""
        spawn_background_task(
            self._turn_store.publish_event({"session_id": session_id, "awaiting_input": awaiting})
        )

    def compact_context(self, session_id: str) -> bool:
        """Trigger a manual compaction of a context's conversation (the user pressed the compact button)."""
        if self._agent_handler() is None:
            return False
        task = asyncio.create_task(self._run_compaction_turn(session_id))
        self._compaction_tasks.add(task)
        task.add_done_callback(self._compaction_tasks.discard)
        return True

    def _agent_handler(self) -> Optional[RequestHandler]:
        """The handler that drives this session's turns, built once on first use."""
        if self._handler is None:
            from a2a.server.request_handlers import DefaultRequestHandler

            # `task_store` is a2a's keyword, not ours — the object it takes is our turn store.
            self._handler = DefaultRequestHandler(agent_executor=self, task_store=self._turn_store)
        return self._handler

    async def _drive_self_sent_turn(
        self, session_id: str, envelope_kind: str, *, metadata_flags: dict,
    ) -> None:
        """Drive one harness-initiated turn through the ordinary turn path via a self-sent agent-role message carrying only a prose-less envelope part, so it is a real, persisted, replayable task streamed to viewers like any other turn."""
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
        """Drive one turn whose only purpose is to say "you have not answered yet"."""
        if self._nudged_to_report:
            return
        self._nudged_to_report = True
        await self._drive_self_sent_turn(
            session_id, REPORT_REMINDER_KIND, metadata_flags={Metadata.REPORT_REMINDER: True},
        )

    async def continue_goal(self, session_id: str) -> None:
        """Open one turn for a goal the session has not finished."""
        await self._drive_self_sent_turn(
            session_id, GOAL_CONTINUATION_KIND, metadata_flags={Metadata.GOAL_CONTINUATION: True},
        )

    def clear_goal(self, session_id: str) -> bool:
        """Call the session's goal off, because the person said so."""
        state = self._contexts.get(session_id)
        runtime = state.runtime if state is not None else None
        if runtime is None or runtime.goal is None:
            return False
        runtime.write_goal(None)
        spawn_background_task(self._persist_session_state(session_id, runtime))
        return True

    async def _persist_session_state(self, session_id: str, runtime: AgentRuntime) -> None:
        """Write the durable session state (goal, tasks) outside a turn."""
        snapshot = runtime.dirty_session_snapshot()
        if snapshot is None:
            return
        try:
            await self._turn_store.save_session_state(session_id, snapshot)
        except Exception:  # noqa: BLE001 — the goal is already off in the live session
            logger.exception("could not persist the session state for %s", session_id)
            return
        runtime.clear_session_dirty()

    def _notify_goal_state(self, session_id: str, goal) -> None:
        """Tell the daemon what this session's goal is now, so the interface can show it and offer to call it off."""
        spawn_background_task(self._turn_store.publish_event({
            "session_id": session_id,
            "goal": goal.public() if goal is not None else None,
        }))

    async def _run_compaction_turn(self, session_id: str) -> None:
        """Drive one manual-compaction turn (a self-sent agent-role compaction message), so it is a real, persisted, replayable task streamed to viewers like any other turn."""
        await self._drive_self_sent_turn(session_id, COMPACTION_KIND, metadata_flags={Metadata.COMPACTION: True})

    def abort_context(self, session_id: str) -> bool:
        # Stop is broadcast to every executor (chat.py), but only the one that actually holds this context's state has anything to stop.
        state = self._contexts.get(session_id)
        if state is None or (state.runtime is None and state.resume_pump is None):
            return False
        # Mark Stopped so the turn's finally (and any later completion) cannot re-arm a resume pump, then cancel the pump already watching it — the autonomous wake is what otherwise revived a fresh, abort-cleared turn seconds after Stop.
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

    def set_locations(self, locations: Optional[list[dict]]) -> int:
        """Adopt the workspace's environments after somebody edited them."""
        self._locations = locations
        for state in self._contexts.values():
            if state.runtime is not None:
                state.runtime.set_locations(locations)
        return len(locations or [])

    async def set_permission_mode(self, mode: str) -> str:
        """Adopt a new permission mode for this session, now."""
        from frank.base.permission_mode import PermissionMode

        resolved = PermissionMode.parse(mode)
        if resolved is None:
            return self._permission_mode
        self._permission_mode = str(resolved)
        # What a runtime built later starts from, and what this session asks for when it creates a peer.
        self._peers.permission_mode = self._permission_mode
        for state in self._contexts.values():
            if state.runtime is not None:
                state.runtime.set_permission_mode(resolved)
        await self._reconsider_parked_gates()
        return self._permission_mode

    async def _reconsider_parked_gates(self) -> None:
        """Re-decide the approvals this session is already stopped on."""
        tasks = await self._turn_store.turns_for_session(self._session_id)
        for task in tasks:
            pending = TurnRecord.from_metadata(task.metadata).pending
            if pending is None or not pending.gates:
                continue
            state = self._contexts.get(task.context_id)
            runtime = state.runtime if state is not None else None
            if runtime is None:
                # No live runtime to ask.
                continue
            for gate in list(pending.gates):
                if gate.request_id in pending.answers:
                    continue
                verdict = await runtime.reconsider_gate(gate)
                if not verdict:
                    continue
                await self.resolve_pending_input({"request_id": gate.request_id, "decision": verdict})

    def reset_runtimes(self) -> None:
        """Drop cached runtimes so the next turn rebuilds them — used after a configuration change (e.g. new API credentials, an mcp.json edit) so it takes effect without a restart."""
        for session_id, state in list(self._contexts.items()):
            if state.runtime is not None:
                state.pending_reset = True
                self._maybe_evict(session_id)

    async def _claim_work_habits_acknowledgement(self, session_id: str, **_flags) -> bool:
        """Whether this turn should emit the once-per-session work-habits acknowledgement."""
        if not self._global_configuration.user_context.enabled:
            return False
        if self._work_habits_acknowledged:
            return False
        claimed = await self._turn_store.claim_work_habits(session_id)
        self._work_habits_acknowledged = True
        return claimed


    def _record_pending_answer(self, task, payload: dict) -> Optional[tuple[dict, dict]]:
        """Record one human answer into the task's durable pending-interaction record and report whether the batch is now fully answered."""
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
        """Drop a single context's cached runtime so the next turn rebuilds it — used when the session's model override changes, so the new model takes effect without a server restart."""
        state = self._contexts.get(session_id)
        if state is None:
            return
        state.pending_reset = True
        self._maybe_evict(session_id)

    def _maybe_evict(self, session_id: str) -> None:
        """Apply a deferred runtime reset once the runtime is idle."""
        state = self._contexts.get(session_id)
        if state is None or not state.pending_reset:
            return
        if state.runtime is not None and state.runtime.has_pending_jobs():
            return  # still delivering — keep the runtime (and its pump) alive
        state.runtime = None
        state.pending_reset = False

    def _context(self, session_id: str) -> _ContextState:
        """The per-context state, created on first access."""
        state = self._contexts.get(session_id)
        if state is None:
            state = _ContextState()
            self._contexts[session_id] = state
        return state

    def teardown_context(self, session_id: str) -> None:
        """Release every trace of a deleted session: cancel its resume pump, abort a live runtime, drop its per-context state, and forget its shared conversation."""
        state = self._contexts.pop(session_id, None)
        if state is not None:
            if state.resume_pump is not None and not state.resume_pump.done():
                state.resume_pump.cancel()
            if state.runtime is not None:
                state.runtime.abort()
        self._conversations.pop(session_id, None)

    def _arm_resume_pump(self, session_id: str, runtime: Optional[AgentRuntime] = None) -> None:
        """Ensure a resume pump watches this context while it has background work in flight."""
        state = self._contexts.get(session_id)
        # A Stopped or torn-down context stays quiet: don't arm a pump that would autonomously wake the agent.
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
        """Wait (event-driven, at zero cost) for each background result to land while the context is otherwise idle, driving an autonomous turn to deliver each."""
        try:
            while True:
                state = self._contexts.get(session_id)
                runtime = state.runtime if state is not None else None
                if runtime is None or not runtime.has_pending_jobs():
                    return
                await runtime.wait_for_jobs()
                # A result landed — drive an autonomous turn to deliver it.
                await self._run_autonomous_turn(session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("background-resume pump failed for context %s", session_id)
        finally:
            # Clear the slot only if it still points at *this* pump — a freshly armed pump (the user resumed) must not be dropped by a late-finishing finally.
            state = self._contexts.get(session_id)
            if state is not None and state.resume_pump is asyncio.current_task():
                state.resume_pump = None
            # The context is idle now, so any reset deferred while it had work in flight can finally take effect (rebuilding with the new configuration).
            self._maybe_evict(session_id)

    async def _run_autonomous_turn(self, session_id: str) -> None:
        """Start a turn the user did not initiate, to deliver a completed background result."""
        if self._agent_handler() is None:
            return
        # Nothing left to deliver — a concurrent user turn already drained the result while the pump was scheduling this wake — so don't even mint a task.
        state = self._contexts.get(session_id)
        runtime = state.runtime if state is not None else None
        has_live_result = runtime is not None and runtime.has_completed_undelivered_jobs()
        has_stored_result = self._job_store.has_undelivered_jobs(session_id, self._agent_name)
        if not has_live_result and not has_stored_result:
            return
        # An agent-authored message (the agent resumed itself), carrying only the prose-less `autonomous_resume` part.
        await self._drive_self_sent_turn(session_id, AUTONOMOUS_RESUME_KIND, metadata_flags={Metadata.AUTONOMOUS_RESUME: True})

    def _build_runtime(
        self,
        session_id: str,
        working_directory: str,
        project_directory: str,
        conversation: Optional[list] = None,
        locations: Optional[list[dict]] = None,
    ) -> AgentRuntime:
        # A worker serves a person's machine, so it gets the machine's catalogue: `~/.agents`, the project's own, the packaged base layer, and the well-known instruction files.
        catalogue = machine_catalogue(self._global_configuration, project_directory)
        configuration = catalogue.agent(self._agent_name)
        if configuration is None:
            available = ", ".join(catalogue.agents()) or "none"
            raise FileNotFoundError(
                f"Agent configuration not found: {self._agent_name} (available: {available})"
            )
        runtime = AgentRuntime(
            agent_configuration=configuration,
            catalogue=catalogue,
            global_configuration=self._global_configuration,
            session_id=session_id,
            conversation=conversation,
            working_directory=working_directory or "",
            project_directory=project_directory or working_directory or "",
            file_lease_manager=self._file_lease_manager,
            locations=locations,
            # Who to answer.
            parent_session=self._parent,
            # The mode this session was created with.
            permission_mode=self._permission_mode,
            sandbox=self._sandbox,
            # The two things the runtime cannot derive from configuration: how this session reaches its peers, and the MCP connections this worker owns.
            session_access=self._peers,
            mcp_manager=self._mcp_manager,
            # The durable job store this worker already holds.
            jobs=self._job_store,
        )
        # No agent-event sink is installed on the runtime any more: `AgentRuntime` stopped carrying one in the package restructure, and path-tagged activity now reaches the stream from `worker/turn.py`, which calls `_on_stream_event` itself.
        runtime.set_turn_reader(self._make_turn_reader())
        # Every goal change reaches the daemon, and through it the interface — which is what makes a goal something the person can see and call off rather than something they infer from the session refusing to go quiet.
        runtime.set_goal_listener(lambda goal: self._notify_goal_state(session_id, goal))
        return runtime

    def _make_turn_reader(self):
        async def read_turn(turn_id: str):
            if not turn_id:
                return None
            task = await self._turn_store.get(turn_id)
            # Exclude `history` for the same reason as the delegate's done payload: read_turn feeds a sibling/agent task straight into the caller's model context, and the full transcript (incl. raw web-search text) would overflow it.
            return task.model_dump(by_alias=True, exclude_none=True, mode="json", exclude={"history"}) if task else None
        return read_turn

    async def _runtime_for(self, session_id: str, workspace: SessionWorktree) -> AgentRuntime:
        # Apply any reset that was deferred while this context had background work in flight: if the runtime has since gone idle, drop it now so this turn rebuilds it with the new configuration rather than reusing the stale one.
        self._maybe_evict(session_id)
        state = self._context(session_id)
        runtime = state.runtime
        if runtime is None:
            # Restore a persisted conversation the first time a context is seen this process (e.g. a session reopened after a restart), so the agent resumes with the same history the UI is replaying rather than a blank slate.
            if session_id not in self._conversations:
                # The model-facing conversation is the task store's per-context checkpoint (the single durable turn surface); a reopened session resumes from it.
                restored = messages_from_dict(await self._turn_store.load_checkpoint(session_id))
                if restored:
                    self._conversations[session_id] = restored
            # Seed from (and bind to) the process-wide dialogue history for this context — the same list object another agent may have been writing to — so a persona switch picks up exactly where the last turn left off.
            conversation = self._conversations.setdefault(session_id, [])
            locations = None
            locations = self._locations
            runtime = self._build_runtime(
                session_id,
                workspace.runtime_working_directory,
                workspace.source_working_directory,
                conversation=conversation,
                locations=locations,
            )
            # Restore the agent's durable objective — its goal and task list — the first time this process builds a runtime for the context (e.g. a session reopened after a restart), alongside the conversation restored above, so a marathon run never loses what it was working toward.
            session_state = await self._turn_store.load_session_state(session_id)
            if session_state:
                runtime.restore_session(session_state)
                # Announce the restored goal, so a session reopened after a restart shows what it is working toward.
                self._notify_goal_state(session_id, runtime.goal)
            state.runtime = runtime
            # First time this process builds a runtime for the context: replay any background results the durable store holds but never delivered (e.g. a parse that finished, or was interrupted, across a restart), so the model sees them as soon as this — or the autonomous wake — runs.
            self._replay_stored_background_results(session_id, runtime)
        # A context's working directory is fixed at creation — a session stays bound to the folder it was started in, so later turns never repoint it.
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
        """After a restart, recover this agent's persisted background work."""
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

    def _workspace(self, requested_working_directory: str = "") -> SessionWorktree:
        """Where this session's work happens."""
        source = requested_working_directory or self._working_directory or ""
        runtime = self._runtime_working_directory or source
        return SessionWorktree(
            source_working_directory=source,
            runtime_working_directory=runtime,
            strategy="worktree" if runtime and runtime != source else "none",
        )

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Run one turn."""
        await _TurnRunner(self, context, event_queue).run()

    async def _suspend_durable_segment(
        self,
        task: Task,
        updater: TaskUpdater,
        interactions: list[SuspensionGate],
        plans: dict,
        save_conversation: Callable[[], Awaitable[None]],
    ) -> bool:
        """Close a top-level turn's A2A segment as a durable suspend."""
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
        """Prepare the session before its socket opens."""
        from frank.base.telemetry import configure as configure_telemetry
        from frank.base.tuning import set_tuning, tuning_from_policy

        configuration = self._global_configuration
        set_tuning(tuning_from_policy(configuration.tuning))
        from frank import _bind_retrieval_policy

        _bind_retrieval_policy(configuration)

        telemetry = configuration.telemetry
        configure_telemetry(
            enabled=telemetry.enabled,
            endpoint=telemetry.exporter.endpoint,
            headers=telemetry.resolved_headers(),
            sample_ratio=telemetry.sample_ratio,
        )

        # Each session connects its own MCP servers.
        servers = configuration.mcp.enabled_servers()
        if servers:
            from frank.base.mcp_client import MCPClientManager

            self._mcp_manager = MCPClientManager(servers)
            # Connected in the background: a hung server must not delay the session's socket, and tool gating keys on the manager existing rather than on live connections.
            self._mcp_connect = asyncio.create_task(self._mcp_manager.start())

        # Every turn tells the model what is on the screen, and the first time a process asks for that listing it spends ~1.8s opening an accessibility connection to each running application — a cost that landed on the session's first turn, before the model had been called at all.
        if self._global_configuration.computer_control.enabled:
            def warm_screen() -> None:
                from frank.computer import targets

                targets.prewarm()

            self._screen_warm = asyncio.create_task(asyncio.to_thread(warm_screen))

        self._context(self._session_id)

    async def start_turn(self, parts: list, metadata: dict) -> str:
        """Start a turn from an inbound message and answer with its task id."""
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
        identified: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def drive() -> None:
            try:
                async for event in handler.on_message_send_stream(MessageSendParams(message=message)):
                    if isinstance(event, Task) and not identified.done():
                        identified.set_result(event.id)
            except Exception as error:  # noqa: BLE001 — a failed turn must still answer the send
                if not identified.done():
                    identified.set_exception(error)
                else:
                    logger.exception("the turn raised after it had started")
            finally:
                if not identified.done():
                    identified.set_result("")

        turn = asyncio.create_task(drive())
        self._startup_resume_tasks.add(turn)
        turn.add_done_callback(self._startup_resume_tasks.discard)
        return await identified

    def _title_from_first_message(self, parts: list) -> None:
        """Name the session after what it was first asked to do."""
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
        """Ask the configured model for a short title, and hand it to the daemon."""
        from langchain_core.messages import HumanMessage, SystemMessage

        from frank.base.configuration import PromptLoader
        from frank.protocol.dtos import SessionTitle
        from frank.runtime.internals import model_is_authorized
        from frank.runtime.runtime import build_chat_model

        try:
            configuration = machine_catalogue(
                self._global_configuration, self._working_directory
            ).agent(self._agent_name)
            if configuration is None:
                return
            model_identifier = configuration.model_identifier
            if not model_identifier or not model_is_authorized(model_identifier, self._global_configuration):
                return
            titling_configuration = configuration.model_copy(update={"reasoning_effort": "low"})
            model = build_chat_model(
                model_identifier, self._global_configuration, titling_configuration,
                self._runtime_working_directory,
            ).bind_tools([SessionTitle], tool_choice="auto")
            prompt = PromptLoader(Path(__file__).resolve().parent.parent / "runtime" / "prompts")
            request = [
                SystemMessage(content=prompt.load("session_title", {})),
                HumanMessage(content=first_message),
            ]
            # The tool is offered, and the prompt is what insists on it.
            attempts = active_tuning().amount(Tunable.session_title_attempts)
            for attempt in range(1, attempts + 1):
                try:
                    response = await model.ainvoke(request)
                except Exception:  # noqa: BLE001 — one bad call is not worth the session's name
                    logger.warning(
                        "naming session %s failed (attempt %d of %d)",
                        self._session_id, attempt, attempts, exc_info=True,
                    )
                    continue
                if not response.tool_calls:
                    logger.warning(
                        "the model answered without calling the title tool for session %s "
                        "(attempt %d of %d)", self._session_id, attempt, attempts,
                    )
                    continue
                title = (SessionTitle.model_validate(response.tool_calls[0]["args"]).title or "").strip()
                if title:
                    await self._turn_store.publish_title(title)
                    return
                logger.warning(
                    "the model returned an empty title for session %s (attempt %d of %d)",
                    self._session_id, attempt, attempts,
                )
            logger.warning("gave up naming session %s after %d attempts", self._session_id, attempts)
        except Exception:  # noqa: BLE001 — a session is not worth failing over its own name
            # Not `debug`.
            logger.warning(
                "could not generate a title for session %s", self._session_id, exc_info=True
            )

    def inject(self, text: str, message_id: str = "", peer_sender: str = "") -> bool:
        """Deliver a message into the turn that is already running, at its next safe point."""
        state = self._contexts.get(self._session_id)
        if state is None or not state.running or state.runtime is None:
            return False
        return state.runtime.enqueue_steering(text, message_id, peer_sender)

    async def resolve_pending_input(self, payload: dict) -> bool:
        """Record a human's answer to a parked gate and resume the turn once every gate in the batch has one."""
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
            logger.exception("resuming session %s after an answer failed", self._session_id)

    def abort(self) -> bool:
        return self.abort_context(self._session_id)

    def abort_tool_call(self, tool_call_identifier: str) -> bool:
        return self.abort_tool(self._session_id, tool_call_identifier)

    async def pending_decision(self) -> str:
        """What this session is parked on, as a sentence, or ``""`` when it is not parked."""
        tasks = await self._turn_store.turns_for_session(self._session_id)
        for task in tasks:
            pending = TurnRecord.from_metadata(task.metadata).pending
            if pending is None or not pending.gates:
                continue
            unanswered = [gate for gate in pending.gates if gate.request_id not in pending.answers]
            if not unanswered:
                continue
            gate = unanswered[0]
            if gate.is_question:
                return "a question it asked the user"
            command = (gate.command or "").strip()
            return f"a permission decision for `{command}`" if command else "a permission decision"
        return ""

    async def abort_pending_input(self) -> bool:
        """Deny every gate this session is parked on."""
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
        """What this session advertises at its well-known path."""
        try:
            return self._build_card_payload()
        except Exception:  # noqa: BLE001 — a card is descriptive, never load-bearing
            logger.exception("building the agent card for session %s failed", self._session_id)
            return {
                "name": self._agent_name,
                "description": f"Frank session {self._session_id}.",
                "version": "1.0.0",
                "protocolVersion": "0.3.0",
                "url": f"unix:{self._session_id}",
                "defaultInputModes": ["text/plain"],
                "defaultOutputModes": ["text/plain"],
                "capabilities": {"streaming": True},
                "skills": [],
            }

    def _build_card_payload(self) -> dict:
        from frank.base.skills import skills_for_agent
        from frank.protocol.card import build_agent_card

        catalogue = machine_catalogue(self._global_configuration, self._working_directory)
        configuration = catalogue.agent(self._agent_name)
        if configuration is None:
            raise FileNotFoundError(f"Agent configuration not found: {self._agent_name}")
        skills = skills_for_agent(list(catalogue.skills()), configuration.skills)
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
        """Stop cleanly: abort any turn in flight so its conversation is checkpointed, cancel the background work it started, close this session's MCP connections, and release the store."""
        import contextlib

        from frank.runtime.background import cancel_all_background_jobs

        self.teardown_context(self._session_id)
        with contextlib.suppress(Exception):
            cancel_all_background_jobs()
        # The browser surface holds a connection to the user's Chrome if a screen tool ever ran.
        if "frank.computer.web" in sys.modules:
            with contextlib.suppress(Exception):
                sys.modules["frank.computer.web"].close()
        if self._screen_warm is not None and not self._screen_warm.done():
            self._screen_warm.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._screen_warm
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
