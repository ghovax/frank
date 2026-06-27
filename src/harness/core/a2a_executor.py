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
import uuid
from typing import Optional

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
            name=skill.name,
            description=skill.description or skill.name,
            tags=["harness", "skill"],
        )
        for skill in available_skills
    ]
    if not skills:
        skills.append(
            AgentSkill(
                id=configuration.identifier,
                name=display_name,
                description=(configuration.description or display_name) + f" {capability}",
                tags=["harness", configuration.permission_mode, configuration.model or "default-model"],
                examples=[f"Ask {display_name} to help with a task in its domain."],
            )
        )
    return AgentCard(
        name=display_name,
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


class HarnessAgentExecutor(AgentExecutor):
    """The A2A executor for a single agent profile. One of these is served per
    agent, so each agent is an independently addressable A2A endpoint."""

    def __init__(
        self,
        agent_name: str,
        global_configuration: GlobalConfiguration,
        task_store: TaskStore,
        pending_permissions: dict[str, asyncio.Future],
        registry: Optional["AgentRegistry"] = None,
        on_new_context: Optional[callable] = None,
    ):
        self._agent_name = agent_name
        self._global_configuration = global_configuration
        self._task_store = task_store
        self._pending_permissions = pending_permissions
        self._registry = registry
        self._on_new_context = on_new_context
        # One runtime per context preserves the conversation across turns.
        self._runtimes: dict[str, AgentRuntime] = {}
        self._aborts: dict[str, AgentRuntime] = {}

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

    def _build_runtime(self, context_id: str, working_directory: str) -> AgentRuntime:
        configuration = load_agent_configuration(
            self._agent_name, self._global_configuration.agent_directories()
        )
        runtime = AgentRuntime(
            agent_configuration=configuration,
            global_configuration=self._global_configuration,
            pending_permissions=self._pending_permissions,
            session_id=context_id,
            working_directory=working_directory or "",
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
            return task.model_dump(by_alias=True, exclude_none=True, mode="json") if task else None
        return read_task

    def _runtime_for(self, context_id: str, working_directory: str) -> AgentRuntime:
        runtime = self._runtimes.get(context_id)
        if runtime is None:
            runtime = self._build_runtime(context_id, working_directory)
            self._runtimes[context_id] = runtime
        elif working_directory:
            runtime._working_directory = working_directory
        return runtime

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        user_text = context.get_user_input()
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

        # The runtime setup — building the agent runtime and its model client —
        # runs inside the try so any failure (e.g. missing API credentials) is
        # surfaced as a clean A2A `failed` status rather than escaping and tearing
        # down the SSE stream mid-flight.
        try:
            await updater.start_work()

            if delegated:
                # A delegated sub-agent call is a fresh, one-shot run (no shared
                # conversation state with the parent turn).
                runtime = self._build_runtime(task.context_id, working_directory)
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

            async for event in runtime.stream(user_text):
                kind = event.type
                data = event.data
                if kind == StreamEvent.Type.TEXT_CHUNK:
                    await emit(_text_part(data.get("text", "")))
                elif kind == StreamEvent.Type.THINKING:
                    await emit(_data_part("thinking", text=data.get("text", ""), label=data.get("label", ""), icon=data.get("icon", "")))
                elif kind == StreamEvent.Type.STATUS:
                    await emit(_data_part("status", code=data.get("code", ""), label=data.get("label", ""), icon=data.get("icon", "")))
                elif kind == StreamEvent.Type.TOOL_CALL:
                    await emit(_data_part(
                        "tool_call", name=data.get("name", ""),
                        arguments=data.get("arguments", {}), toolCallId=data.get("id", ""),
                    ))
                elif kind == StreamEvent.Type.TOOL_RESULT:
                    await emit(_data_part(
                        "tool_result", name=data.get("name", ""),
                        result=data.get("result"), toolCallId=data.get("id", ""),
                    ))
                elif kind == StreamEvent.Type.PERMISSION_REQUEST:
                    await emit(_data_part(
                        "permission_request", requestId=data.get("request_id", ""),
                        command=data.get("command", ""), justification=data.get("justification", ""),
                        risk=data.get("risk", ""),
                    ))
                elif kind == StreamEvent.Type.ERROR:
                    failed_message = data.get("message", "error")
                    await emit(_data_part("error", message=failed_message))
                elif kind == StreamEvent.Type.AGENT_GROUP_STARTED:
                    await emit(_data_part(
                        "agent_group_started",
                        groupId=data.get("group_id", ""),
                        toolCallId=data.get("tool_call_id", ""),
                        justification=data.get("justification", ""),
                        steps=data.get("steps", []),
                    ))
                elif kind == StreamEvent.Type.AGENT_TEXT_CHUNK:
                    await emit_sub("sub_task_text", data, text=data.get("text", ""))
                elif kind == StreamEvent.Type.AGENT_THINKING:
                    await emit_sub("sub_task_thinking", data, text=data.get("text", ""), label=data.get("label", ""), icon=data.get("icon", ""))
                elif kind == StreamEvent.Type.AGENT_TOOL_CALL:
                    await emit_sub("sub_task_tool_call", data, name=data.get("name", ""), arguments=data.get("arguments", {}), toolCallId=data.get("toolCallId", ""))
                elif kind == StreamEvent.Type.AGENT_TOOL_RESULT:
                    await emit_sub("sub_task_tool_result", data, name=data.get("name", ""), result=data.get("result"), toolCallId=data.get("toolCallId", ""))
                elif kind == StreamEvent.Type.AGENT_STATUS:
                    await emit_sub("sub_task_status", data, code=data.get("code", ""), label=data.get("label", ""), icon=data.get("icon", ""))
                elif kind == StreamEvent.Type.AGENT_DONE:
                    await emit_sub("sub_task_done", data, task=data.get("task"))
                elif kind == StreamEvent.Type.DONE:
                    final_text = data.get("text", "") or final_text

            if final_text.strip():
                await updater.add_artifact([_text_part(final_text)], name="result", last_chunk=True)
            if failed_message and not final_text.strip():
                await updater.failed(updater.new_agent_message([_text_part(failed_message)]))
            else:
                await updater.complete()
        except Exception as exception:  # noqa: BLE001 — surface any failure as A2A failed
            await updater.failed(updater.new_agent_message([_text_part(f"Execution error: {exception}")]))
        finally:
            self._aborts.pop(task.id, None)

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
        ):
            handler = self._handlers.get(agent_name)
            if handler is None:
                yield {"type": "done", "child_task_id": "", "task": None}
                return
            metadata: dict = {DELEGATED_METADATA_KEY: True, DEPTH_METADATA_KEY: depth}
            if read_only is not None:
                metadata[READ_ONLY_METADATA_KEY] = bool(read_only)
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
                                    yield {"type": "thinking", "text": root.data.get("text", ""), "label": root.data.get("label", ""), "icon": root.data.get("icon", ""), "child_task_id": child_task_id}
                                elif data_kind == "status":
                                    yield {"type": "status", "code": root.data.get("code", ""), "label": root.data.get("label", ""), "icon": root.data.get("icon", ""), "child_task_id": child_task_id}
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
                elif isinstance(event, TaskArtifactUpdateEvent):
                    child_task_id = event.task_id or child_task_id
            final_task = await self._task_store.get(child_task_id) if child_task_id else None
            yield {
                "type": "done",
                "child_task_id": child_task_id,
                "task": final_task.model_dump(by_alias=True, exclude_none=True, mode="json") if final_task else None,
            }

        return delegate
