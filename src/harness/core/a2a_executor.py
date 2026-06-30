"""A2A server adapter for the harness.

Bridges the harness's :class:`AgentRuntime` to the A2A protocol: a user
turn is an A2A *Task*, its progress is streamed as ``TaskStatusUpdateEvent`` /
``TaskArtifactUpdateEvent`` (via :class:`TaskUpdater`), and its deliverable is an
``Artifact``. Sub-agents (spawned via A2A) become *related* A2A tasks sharing
the parent's ``contextId`` and referencing it through ``referenceTaskIds``; they
are persisted to the same :class:`TaskStore`, so each is independently fetchable
and replayable via ``tasks/get``.

The harness's own event vocabulary (text/thinking/tool calls/sub-agent activity)
is carried as typed ``Part``s — ``TextPart`` for prose, ``DataPart`` for
everything structured (tool calls/results, sub-task lifecycle, permission
prompts) — so the live stream is fully A2A-shaped, not a bespoke side channel.
"""

import asyncio
import json
import uuid
from typing import Awaitable, Callable, Optional

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers.request_handler import RequestHandler
from a2a.server.tasks import TaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    DataPart,
    Message,
    MessageSendParams,
    Part,
    Role,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
)
from a2a.utils import new_task

from harness.core.agent import AgentRuntime, StreamEvent
from harness.core.configuration import (
    AgentConfiguration,
    GlobalConfiguration,
    load_agent_configuration,
)
from harness.core.skills import Skill

# Metadata key the client may set to steer the working directory of a turn.
WORKING_DIRECTORY_METADATA_KEY = "harness/workingDirectory"
# Marks a sub-agent call delegated from another agent (one-shot, fresh state).
DELEGATED_METADATA_KEY = "harness/delegated"
# Forces a delegated sub-agent into read-only mode for this call.
READ_ONLY_METADATA_KEY = "harness/readOnly"
# Sets the permission mode for a top-level user turn.
PERMISSION_MODE_METADATA_KEY = "harness/permissionMode"
# The delegation depth of a sub-agent call (how many hops from the chat agent).
DEPTH_METADATA_KEY = "harness/depth"
# DataPart discriminator: every structured part declares its kind in `data.kind`.
PART_KIND = "kind"
# A user turn whose input is a widget interaction carries it as a DataPart of
# this kind rather than as prose, so the payload reaches the model intact.
WIDGET_EVENT_KIND = "widget_event"


def _widget_event_payload(message) -> Optional[dict]:
    """The incoming widget-interaction DataPart, if this turn carries one. Returns
    ``None`` when the message has no widget event, so the caller falls back to the
    plain text input."""
    for part in (message.parts or []):
        root = getattr(part, "root", part)
        if isinstance(root, DataPart) and root.data.get(PART_KIND) == WIDGET_EVENT_KIND:
            return {
                "artifact_id": root.data.get("artifactId", ""),
                "title": root.data.get("title", ""),
                "event": root.data.get("event", ""),
                "data": root.data.get("data"),
            }
    return None

# Each agent profile is served as its own A2A agent under this prefix.
AGENT_RPC_PREFIX = "/a2a/agents"


def agent_rpc_path(agent_name: str) -> str:
    return f"{AGENT_RPC_PREFIX}/{agent_name}"


def build_agent_card(configuration: AgentConfiguration, available_skills: list[Skill], base_url: str) -> AgentCard:
    """Compile an agent's markdown definition into its A2A AgentCard — the
    A2A-native way to broadcast an agent's identity and capabilities. Each
    profile becomes an independently addressable, discoverable A2A agent.

    The agent's available skills (discovered from the skills directory) are
    advertised on the card; if there are none, a single default skill describing
    the agent's role is synthesised so the card always carries at least one skill.
    """
    display_name = configuration.display_name
    capability = (
        "Investigates and reports read-only — cannot modify the system."
        if configuration.permission_mode == "read_only"
        else "Can read and modify the system."
    )
    skills = [
        AgentSkill(
            id=skill.identifier,
            name=skill.identifier,
            description=skill.description or skill.display_title,
            tags=["harness", "skill"],
        )
        for skill in available_skills
    ]
    if not skills:
        skills.append(
            AgentSkill(
                id=configuration.identifier,
                name=configuration.identifier,
                description=(configuration.description or display_name) + f" {capability}",
                tags=["harness", configuration.permission_mode, configuration.model or "default-model"],
                examples=[f"Ask {display_name} to help with a task in its domain."],
            )
        )
    return AgentCard(
        name=configuration.identifier,
        description=configuration.description or f"The '{display_name}' agent.",
        url=f"{base_url.rstrip('/')}{agent_rpc_path(configuration.identifier)}",
        version="1.0.0",
        protocol_version="0.3.0",
        preferred_transport="JSONRPC",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True, state_transition_history=True),
        skills=skills,
    )


def _text_part(text: str) -> Part:
    return Part(root=TextPart(text=text))


def _data_part(kind: str, **fields) -> Part:
    return Part(root=DataPart(data={PART_KIND: kind, **fields}))


class _TextPartBuffer:
    """Coalesce adjacent text chunks before publishing A2A task updates.

    The buffering is intentionally at the semantic event layer, not the SSE/ASGI
    layer: structured parts such as tool calls, status changes, and sub-agent
    lifecycle events must force a flush so replay order remains exact.
    """

    def __init__(
        self,
        emit: Callable[[tuple[str, ...], str], Awaitable[None]],
        *,
        flush_interval: float = 0.05,
        flush_size: int = 512,
    ):
        self._emit = emit
        self._flush_interval = flush_interval
        self._flush_size = flush_size
        self._key: tuple[str, ...] | None = None
        self._chunks: list[str] = []
        self._length = 0
        self._last_flush = 0.0

    async def push(self, text: str, key: tuple[str, ...] = ()) -> None:
        if not text:
            return
        if self._chunks and self._key != key:
            await self.flush(force=True)
        if not self._chunks:
            self._key = key
            self._last_flush = asyncio.get_running_loop().time()
        self._chunks.append(text)
        self._length += len(text)
        await self.flush()

    async def flush(self, force: bool = False) -> None:
        if not self._chunks or self._key is None:
            return
        now = asyncio.get_running_loop().time()
        if not force and self._length < self._flush_size and now - self._last_flush < self._flush_interval:
            return
        key = self._key
        text = "".join(self._chunks)
        self._key = None
        self._chunks = []
        self._length = 0
        self._last_flush = now
        await self._emit(key, text)


class HarnessAgentExecutor(AgentExecutor):
    """The A2A executor for a single agent profile. One of these is served per
    agent, so each agent is an independently addressable A2A endpoint."""

    def __init__(
        self,
        agent_name: str,
        global_configuration: GlobalConfiguration,
        task_store: TaskStore,
        pending_permissions: dict[str, asyncio.Future],
        pending_questions: dict[str, asyncio.Future],
        registry: Optional["AgentRegistry"] = None,
        on_new_context: Optional[callable] = None,
        conversations: Optional[dict[str, list]] = None,
        on_turn_state: Optional[callable] = None,
        on_permission_state: Optional[callable] = None,
        load_conversation: Optional[callable] = None,
        save_conversation: Optional[callable] = None,
        session_model_for: Optional[callable] = None,
    ):
        self._agent_name = agent_name
        self._global_configuration = global_configuration
        self._task_store = task_store
        self._pending_permissions = pending_permissions
        self._pending_questions = pending_questions
        self._registry = registry
        self._on_new_context = on_new_context
        # Persist/restore the dialogue history so a session keeps its context across
        # a server restart. ``_conversations`` is in-memory; without these the agent
        # would resume a reopened session with an empty history while the UI still
        # replays the transcript from the task store, silently losing all context.
        self._load_conversation = load_conversation
        self._save_conversation = save_conversation
        # Resolves a context's persisted per-session model override (provider/model
        # id, or "" for the global default), so a runtime is built with the model
        # the user chose for that conversation rather than only the global default.
        self._session_model_for = session_model_for
        # Notified (context_id, running) when a top-level turn starts/ends, so the
        # server can track which sessions are active and show a sidebar spinner.
        self._on_turn_state = on_turn_state
        # Notified (context_id) when a turn raises a permission request, so the
        # sidebar can swap the spinner for an attention marker on that session.
        self._on_permission_state = on_permission_state
        # One runtime per context preserves the conversation across turns.
        self._runtimes: dict[str, AgentRuntime] = {}
        self._aborts: dict[str, AgentRuntime] = {}
        self._active_contexts: set[str] = set()
        # Dialogue history keyed by context, shared across *all* agent executors
        # in the process. The persona (system prompt) is applied per-turn on top
        # of this, so switching agents mid-session continues the same conversation
        # with a different persona rather than starting over.
        self._conversations: dict[str, list] = conversations if conversations is not None else {}

    def abort_context(self, context_id: str) -> bool:
        runtime = self._runtimes.get(context_id)
        if runtime is None:
            return False
        runtime.abort()
        return True

    def reset_runtimes(self) -> None:
        """Drop cached runtimes so the next turn rebuilds them — used after a
        configuration change (e.g. new API credentials) so it takes effect without
        a restart. In-flight turns keep their own runtime reference and are
        unaffected."""
        self._runtimes.clear()

    def set_permission_mode(self, context_id: str, mode: str) -> bool:
        runtime = self._runtimes.get(context_id)
        if runtime is None:
            return False
        runtime.set_permission_mode(mode)
        return True

    def steer_context(self, context_id: str, message: str) -> bool:
        if context_id not in self._active_contexts:
            return False
        runtime = self._runtimes.get(context_id)
        if runtime is None:
            return False
        return runtime.enqueue_steering(message)

    def reset_runtime(self, context_id: str) -> None:
        """Drop a single context's cached runtime so the next turn rebuilds it —
        used when the session's model override changes, so the new model takes
        effect without a server restart."""
        self._runtimes.pop(context_id, None)

    def _build_runtime(
        self,
        context_id: str,
        working_directory: str,
        conversation: Optional[list] = None,
        is_sub_agent: bool = False,
        model_override: Optional[str] = None,
    ) -> AgentRuntime:
        configuration = load_agent_configuration(
            self._agent_name, self._global_configuration.agent_directories_for(working_directory)
        )
        # A per-session model override wins over the agent's own ``model`` field
        # and the global default. Applied on a copy so the loaded agent config is
        # not mutated for other contexts.
        if model_override:
            configuration = configuration.model_copy(update={"model": model_override})
        runtime = AgentRuntime(
            agent_configuration=configuration,
            global_configuration=self._global_configuration,
            pending_permissions=self._pending_permissions,
            pending_questions=self._pending_questions,
            session_id=context_id,
            conversation=conversation,
            working_directory=working_directory or "",
            is_sub_agent=is_sub_agent,
        )
        if self._registry is not None:
            runtime.set_delegate(self._registry.make_delegate(context_id))
        runtime.set_task_reader(self._make_task_reader())
        return runtime

    def _make_task_reader(self):
        async def read_task(task_id: str):
            if not task_id:
                return None
            task = await self._task_store.get(task_id)
            # Exclude `history` for the same reason as the delegate's done payload:
            # read_task feeds a sibling/sub-agent task straight into the caller's
            # model context, and the full transcript (incl. raw web-search text)
            # would overflow it. Only the status + deliverable artifact are needed.
            return task.model_dump(by_alias=True, exclude_none=True, mode="json", exclude={"history"}) if task else None
        return read_task

    def _runtime_for(self, context_id: str, working_directory: str) -> AgentRuntime:
        runtime = self._runtimes.get(context_id)
        if runtime is None:
            # Restore a persisted conversation the first time a context is seen this
            # process (e.g. a session reopened after a restart), so the agent resumes
            # with the same history the UI is replaying rather than a blank slate.
            if context_id not in self._conversations and self._load_conversation is not None:
                restored = self._load_conversation(context_id)
                if restored:
                    self._conversations[context_id] = restored
            # Seed from (and bind to) the process-wide dialogue history for this
            # context — the same list object another agent may have been writing
            # to — so a persona switch picks up exactly where the last turn left off.
            conversation = self._conversations.setdefault(context_id, [])
            model_override = ""
            if self._session_model_for is not None:
                model_override = (self._session_model_for(context_id) or "").strip()
            runtime = self._build_runtime(
                context_id,
                working_directory,
                conversation=conversation,
                model_override=model_override or None,
            )
            self._runtimes[context_id] = runtime
        # A context's working directory is fixed at creation — a session stays
        # bound to the folder it was started in, so later turns never repoint it.
        return runtime

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_text = context.get_user_input()
        # A widget interaction arrives as a structured DataPart. A normal
        # interaction becomes the turn's JSON input; a render_error is reframed
        # below (once the runtime's prompt loader exists) into a behind-the-scenes
        # self-realization note the model repairs as its own output.
        widget_payload = _widget_event_payload(context.message)
        metadata = context.message.metadata or {}
        working_directory = metadata.get(WORKING_DIRECTORY_METADATA_KEY, "")
        permission_mode = str(metadata.get(PERMISSION_MODE_METADATA_KEY, ""))
        delegated = bool(metadata.get(DELEGATED_METADATA_KEY))

        task = context.current_task
        if task is None:
            task = new_task(context.message)
            # Link a delegated child to its parent task so the relationship is
            # discoverable on the persisted A2A task.
            reference_task_ids = context.message.reference_task_ids
            if reference_task_ids:
                task.metadata = {**(task.metadata or {}), "referenceTaskIds": reference_task_ids}
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        final_text = ""
        failed_message = ""
        runtime: AgentRuntime | None = None

        # A top-level user turn marks its session as running so the sidebar can
        # show a spinner. Delegated sub-agent turns run within their parent turn,
        # which is already counted, so they are not tracked separately.
        track_running = not delegated and self._on_turn_state is not None
        if track_running:
            self._on_turn_state(task.context_id, True)
            self._active_contexts.add(task.context_id)

        async def emit(part: Part) -> None:
            await updater.update_status(TaskState.working, updater.new_agent_message([part]))

        # Re-publishes a sub-agent's activity onto this task's stream for the live
        # UI. The sub-agent is itself a real A2A task (created and persisted by its
        # own served endpoint via delegation); the child task id arrives on the
        # event, so nothing is assigned or persisted here.
        async def emit_sub(kind: str, data: dict, **fields) -> None:
            await emit(_data_part(
                kind,
                groupId=data.get("group_id", ""),
                stepId=data.get("step_id", ""),
                childTaskId=data.get("child_task_id", ""),
                **fields,
            ))

        async def emit_text_buffer(_key: tuple[str, ...], text: str) -> None:
            await emit(_text_part(text))

        async def emit_sub_text_buffer(key: tuple[str, ...], text: str) -> None:
            group_id, step_id, child_task_id = key
            await emit(_data_part(
                "sub_task_text",
                groupId=group_id,
                stepId=step_id,
                childTaskId=child_task_id,
                text=text,
            ))

        text_buffer = _TextPartBuffer(emit_text_buffer)
        sub_text_buffer = _TextPartBuffer(emit_sub_text_buffer)

        async def flush_stream_buffers(force: bool = True) -> None:
            await text_buffer.flush(force=force)
            await sub_text_buffer.flush(force=force)

        def save_runtime_conversation() -> None:
            if not delegated and self._save_conversation is not None and runtime is not None:
                self._save_conversation(task.context_id, runtime.conversation)

        # The runtime setup — building the agent runtime and its model client —
        # runs inside the try so any failure (e.g. missing API credentials) is
        # surfaced as a clean A2A `failed` status rather than escaping and tearing
        # down the SSE stream mid-flight.
        try:
            await updater.start_work()

            if delegated:
                # A delegated sub-agent call is a fresh, one-shot run (no shared
                # conversation state with the parent turn).
                runtime = self._build_runtime(task.context_id, working_directory, is_sub_agent=True)
                if READ_ONLY_METADATA_KEY in metadata:
                    runtime.set_read_only(bool(metadata[READ_ONLY_METADATA_KEY]))
            else:
                is_new_context = task.context_id not in self._runtimes
                runtime = self._runtime_for(task.context_id, working_directory)
                if permission_mode:
                    runtime.set_permission_mode(permission_mode)
                if is_new_context and self._on_new_context is not None:
                    self._on_new_context(task.context_id, self._agent_name, working_directory, user_text)
            self._aborts[task.id] = runtime
            runtime.set_a2a_task_id(task.id)
            runtime.set_delegation_depth(int(metadata.get(DEPTH_METADATA_KEY, 0)))

            # A render_error is injected as the model's own realization (a system
            # note), never as a user message; every other widget event is the
            # turn's structured JSON input.
            as_system_note = False
            if widget_payload is not None:
                payload_json = json.dumps({"widget_event": widget_payload}, ensure_ascii=False)
                if widget_payload.get("event") == "render_error":
                    # The same JSON payload, wrapped in a self-realization note and
                    # injected as a system message rather than user input.
                    turn_input = runtime.preview_render_error_note(payload_json)
                    as_system_note = True
                else:
                    turn_input = payload_json
            else:
                turn_input = user_text

            async for event in runtime.stream(turn_input, as_system_note=as_system_note):
                kind = event.type
                data = event.data
                if kind == StreamEvent.Type.TEXT_CHUNK:
                    await text_buffer.push(data.get("text", ""))
                elif kind == StreamEvent.Type.THINKING:
                    await flush_stream_buffers()
                    await emit(_data_part("thinking", text=data.get("text", "")))
                elif kind == StreamEvent.Type.THINKING_DONE:
                    await flush_stream_buffers()
                    await emit(_data_part("thinking_done", durationMs=data.get("duration_ms", 0)))
                elif kind == StreamEvent.Type.STATUS:
                    await flush_stream_buffers()
                    await emit(_data_part("status", code=data.get("code", "")))
                elif kind == StreamEvent.Type.TOOL_CALL:
                    await flush_stream_buffers()
                    await emit(_data_part(
                        "tool_call", name=data.get("name", ""),
                        arguments=data.get("arguments", {}), toolCallId=data.get("id", ""),
                    ))
                elif kind == StreamEvent.Type.TOOL_RESULT:
                    await flush_stream_buffers()
                    await emit(_data_part(
                        "tool_result", name=data.get("name", ""),
                        result=data.get("result"), toolCallId=data.get("id", ""),
                    ))
                elif kind == StreamEvent.Type.MCP_EVENT:
                    await flush_stream_buffers()
                    await emit(_data_part(
                        "mcp_event",
                        name=data.get("name", ""),
                        server=data.get("server", ""),
                        tool=data.get("tool", ""),
                        event=data.get("event", {}),
                        toolCallId=data.get("id", ""),
                    ))
                elif kind == StreamEvent.Type.PERMISSION_REQUEST:
                    await flush_stream_buffers()
                    await emit(_data_part(
                        "permission_request", requestId=data.get("request_id", ""),
                        toolCallId=data.get("id", ""),
                        command=data.get("command", ""), justification=data.get("justification", ""),
                        risk=data.get("risk", ""),
                    ))
                    # Let the sidebar flag this session as awaiting input.
                    if self._on_permission_state is not None:
                        self._on_permission_state(task.context_id)
                elif kind == StreamEvent.Type.QUESTION:
                    await flush_stream_buffers()
                    await emit(_data_part(
                        "question",
                        requestId=data.get("request_id", ""),
                        toolCallId=data.get("id", ""),
                        questions=data.get("questions", []) or [],
                    ))
                    # A question is human-in-the-loop input, same as a permission.
                    if self._on_permission_state is not None:
                        self._on_permission_state(task.context_id)
                elif kind == StreamEvent.Type.ERROR:
                    await flush_stream_buffers()
                    failed_message = data.get("message", "error")
                    await emit(_data_part(
                        "error",
                        message=failed_message,
                        toolCallId=data.get("id", ""),
                        name=data.get("tool", ""),
                    ))
                elif kind == StreamEvent.Type.AGENT_GROUP_STARTED:
                    await flush_stream_buffers()
                    await emit(_data_part(
                        "agent_group_started",
                        groupId=data.get("group_id", ""),
                        toolCallId=data.get("tool_call_id", ""),
                        steps=data.get("steps", []),
                    ))
                elif kind == StreamEvent.Type.AGENT_TEXT_CHUNK:
                    await text_buffer.flush(force=True)
                    await sub_text_buffer.push(
                        data.get("text", ""),
                        (
                            data.get("group_id", ""),
                            data.get("step_id", ""),
                            data.get("child_task_id", ""),
                        ),
                    )
                elif kind == StreamEvent.Type.AGENT_THINKING:
                    await flush_stream_buffers()
                    await emit_sub("sub_task_thinking", data, text=data.get("text", ""))
                elif kind == StreamEvent.Type.AGENT_THINKING_DONE:
                    await flush_stream_buffers()
                    await emit_sub("sub_task_thinking_done", data, durationMs=data.get("duration_ms", 0))
                elif kind == StreamEvent.Type.AGENT_TOOL_CALL:
                    await flush_stream_buffers()
                    await emit_sub("sub_task_tool_call", data, name=data.get("name", ""), arguments=data.get("arguments", {}), toolCallId=data.get("toolCallId", ""))
                elif kind == StreamEvent.Type.AGENT_TOOL_RESULT:
                    await flush_stream_buffers()
                    await emit_sub("sub_task_tool_result", data, name=data.get("name", ""), result=data.get("result"), toolCallId=data.get("toolCallId", ""))
                elif kind == StreamEvent.Type.AGENT_MCP_EVENT:
                    await flush_stream_buffers()
                    await emit_sub("sub_task_mcp_event", data, event=data.get("event", {}), toolCallId=data.get("toolCallId", ""))
                elif kind == StreamEvent.Type.AGENT_STATUS:
                    await flush_stream_buffers()
                    await emit_sub("sub_task_status", data, code=data.get("code", ""))
                elif kind == StreamEvent.Type.AGENT_DONE:
                    await flush_stream_buffers()
                    await emit_sub("sub_task_done", data, task=data.get("task"))
                elif kind == StreamEvent.Type.TASKS_UPDATED:
                    await flush_stream_buffers()
                    await emit(_data_part(
                        "tasks_updated",
                        toolCallId=data.get("id", ""),
                        tasks=data.get("tasks", []),
                        resultMessage=data.get("result_message", ""),
                    ))
                elif kind == StreamEvent.Type.STEERING:
                    await flush_stream_buffers()
                    await emit(_data_part("steering", text=data.get("text", "")))
                elif kind == StreamEvent.Type.DONE:
                    await flush_stream_buffers()
                    final_text = data.get("text", "") or final_text

            await flush_stream_buffers()

            if final_text.strip():
                await updater.add_artifact([_text_part(final_text)], name="result", last_chunk=True)
            save_runtime_conversation()
            if failed_message and not final_text.strip():
                await updater.failed(updater.new_agent_message([_text_part(failed_message)]))
            else:
                await updater.complete()
        except Exception as exception:  # noqa: BLE001 — surface any failure as A2A failed
            save_runtime_conversation()
            await updater.failed(updater.new_agent_message([_text_part(f"Execution error: {exception}")]))
        finally:
            self._aborts.pop(task.id, None)
            # Persist the conversation after a top-level turn so a later restart can
            # restore it. Delegated sub-agent runs have their own throwaway history
            # and don't touch the shared context, so they are not persisted.
            if not delegated and self._save_conversation is not None:
                self._save_conversation(
                    task.context_id,
                    runtime.conversation if runtime is not None else self._conversations.get(task.context_id, []),
                )
            if track_running:
                self._active_contexts.discard(task.context_id)
                self._on_turn_state(task.context_id, False)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or (context.current_task.id if context.current_task else "")
        runtime = self._aborts.get(task_id)
        if runtime is not None:
            runtime.abort()
        if context.current_task:
            updater = TaskUpdater(event_queue, context.current_task.id, context.current_task.context_id)
            await updater.cancel()


class AgentRegistry:
    """The A2A agent directory.

    Holds every served agent's request handler and AgentCard, and provides A2A
    *delegation*: one agent invokes another by sending it a real A2A message
    (``message/stream``) and receiving its task back. This is how agents
    communicate — there is no separate, custom inter-agent channel; a sub-agent
    call is identical to an external client calling that agent's endpoint.
    """

    def __init__(self, task_store: TaskStore):
        self._task_store = task_store
        self._handlers: dict[str, RequestHandler] = {}
        self._cards: dict[str, AgentCard] = {}

    def register(self, name: str, handler: RequestHandler, card: AgentCard) -> None:
        self._handlers[name] = handler
        self._cards[name] = card

    def names(self) -> list[str]:
        return list(self._cards.keys())

    def cards(self) -> list[AgentCard]:
        return list(self._cards.values())

    def card(self, name: str) -> Optional[AgentCard]:
        return self._cards.get(name)

    def make_delegate(self, context_id: str):
        """Return a delegate bound to a context. Calling it invokes another agent
        as a related A2A task and yields its activity as it streams, ending with
        the child's final task."""

        async def delegate(
            agent_name: str,
            prompt: str,
            parent_task_id: str,
            read_only: Optional[bool] = None,
            depth: int = 1,
            working_directory: str = "",
        ):
            handler = self._handlers.get(agent_name)
            if handler is None:
                yield {"type": "done", "child_task_id": "", "task": None}
                return
            metadata: dict = {DELEGATED_METADATA_KEY: True, DEPTH_METADATA_KEY: depth}
            if read_only is not None:
                metadata[READ_ONLY_METADATA_KEY] = bool(read_only)
            if working_directory:
                metadata[WORKING_DIRECTORY_METADATA_KEY] = working_directory
            message = Message(
                role=Role.user,
                parts=[Part(root=TextPart(text=prompt))],
                message_id=uuid.uuid4().hex,
                context_id=context_id,
                reference_task_ids=[parent_task_id] if parent_task_id else None,
                metadata=metadata,
            )
            child_task_id = ""
            async for event in handler.on_message_send_stream(MessageSendParams(message=message)):
                if isinstance(event, Task):
                    child_task_id = event.id
                    yield {"type": "started", "child_task_id": child_task_id}
                elif isinstance(event, TaskStatusUpdateEvent):
                    child_task_id = event.task_id or child_task_id
                    if event.status.message:
                        for part in event.status.message.parts:
                            root = part.root
                            if isinstance(root, TextPart):
                                yield {"type": "text", "text": root.text, "child_task_id": child_task_id}
                            elif isinstance(root, DataPart):
                                data_kind = root.data.get(PART_KIND)
                                if data_kind == "thinking":
                                    yield {"type": "thinking", "text": root.data.get("text", ""), "child_task_id": child_task_id}
                                elif data_kind == "thinking_done":
                                    yield {"type": "thinking_done", "durationMs": root.data.get("durationMs", 0), "child_task_id": child_task_id}
                                elif data_kind == "status":
                                    yield {"type": "status", "code": root.data.get("code", ""), "child_task_id": child_task_id}
                                elif data_kind == "tool_call":
                                    yield {
                                        "type": "tool_call",
                                        "name": root.data.get("name", ""),
                                        "arguments": root.data.get("arguments", {}),
                                        "toolCallId": root.data.get("toolCallId", ""),
                                        "child_task_id": child_task_id,
                                    }
                                elif data_kind == "tool_result":
                                    yield {
                                        "type": "tool_result",
                                        "name": root.data.get("name", ""),
                                        "result": root.data.get("result"),
                                        "toolCallId": root.data.get("toolCallId", ""),
                                        "child_task_id": child_task_id,
                                    }
                                elif data_kind == "mcp_event":
                                    yield {
                                        "type": "mcp_event",
                                        "event": root.data.get("event", {}),
                                        "toolCallId": root.data.get("toolCallId", ""),
                                        "child_task_id": child_task_id,
                                    }
                                elif data_kind == "error":
                                    yield {
                                        "type": "error",
                                        "message": root.data.get("message", ""),
                                        "name": root.data.get("name", ""),
                                        "toolCallId": root.data.get("toolCallId", ""),
                                        "child_task_id": child_task_id,
                                    }
                elif isinstance(event, TaskArtifactUpdateEvent):
                    child_task_id = event.task_id or child_task_id
            final_task = await self._task_store.get(child_task_id) if child_task_id else None
            yield {
                "type": "done",
                "child_task_id": child_task_id,
                # Hand back only the sub-agent's deliverable (status + result artifact),
                # never its `history`. The history holds every relayed event — including
                # full web-search page text — and the parent serializes this task into
                # its own model context as the spawn_agent result; injecting the whole
                # transcript overflows the context window. The live agents panel is fed
                # by the separate streamed events above, so it is unaffected, and the UI
                # result card only reads the artifact.
                "task": final_task.model_dump(by_alias=True, exclude_none=True, mode="json", exclude={"history"}) if final_task else None,
            }

        return delegate
