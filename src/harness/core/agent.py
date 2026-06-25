import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

from langchain_openai import ChatOpenAI
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool
from pydantic import BaseModel


class ReasoningChatOpenAI(ChatOpenAI):
    """ChatOpenAI subclass that preserves reasoning_content across turns.

    DeepSeek-style models return reasoning_content alongside content.
    LangChain's default serialization drops it. This subclass injects it
    back into the API payload so the model sees its own prior reasoning.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        messages = self._convert_input(input_).to_messages()
        for payload_message, source_message in zip(
            payload.get("messages", []), messages, strict=False
        ):
            if not isinstance(source_message, (AIMessage, AIMessageChunk)):
                continue
            reasoning = source_message.additional_kwargs.get("reasoning_content", "")
            if reasoning:
                payload_message["reasoning_content"] = reasoning
        return payload

from harness.core.configuration import (
    AgentConfiguration,
    GlobalConfiguration,
    PermissionEvaluator,
    PermissionError,
    PromptLoader,
    load_agent_configuration,
    list_available_agents,
)
from harness.tools.tools import (
    bash as bash_tool,
    web_search as web_search_tool,
    spawn_agent as spawn_tool,
    write_tasks as write_tasks_tool,
    update_tasks as update_tasks_tool,
    orchestrate as orchestrate_tool,
    register_spawned_task,
    cancel_all_background_tasks,
    bash_tasks,
    web_tasks,
    spawned_tasks,
)

from harness.core.orchestrator_graph import (
    compile_orchestration_graph,
    OrchestrationState,
)


class StreamEvent:
    class Type(str, Enum):
        SESSION = "session"
        STATUS = "status"
        THINKING = "thinking"
        TEXT_CHUNK = "text_chunk"
        TOOL_CALL = "tool_call"
        TOOL_RESULT = "tool_result"
        DONE = "done"
        BACKGROUND_STARTED = "background_started"
        PERMISSION_REQUEST = "permission_request"
        TASKS_UPDATED = "tasks_updated"
        ERROR = "error"
        DENIED_INJECTION = "denied_injection"
        ORCHESTRATION_STARTED = "orchestration_started"
        AGENT_TEXT_CHUNK = "agent_text_chunk"
        AGENT_TOOL_CALL = "agent_tool_call"
        AGENT_THINKING = "agent_thinking"
        AGENT_STATUS = "agent_status"
        AGENT_DONE = "agent_done"

    def __init__(self, event_type: Type, **data):
        self.type = event_type
        self.data = data

    def to_dict(self) -> dict:
        return {"type": self.type.value, "timestamp": datetime.now(timezone.utc).isoformat(), **self.data}

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


_agent_event_queues: dict[str, asyncio.Queue["StreamEvent | None"]] = {}


def _maybe_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _build_tools(tools_configuration) -> list[BaseTool]:
    available = [bash_tool, web_search_tool, write_tasks_tool, update_tasks_tool, orchestrate_tool]
    if tools_configuration.spawn_agent.enabled:
        available.append(spawn_tool)
    return available


class SubAgentRunner:
    def __init__(
        self,
        agent_configuration: AgentConfiguration,
        global_configuration: GlobalConfiguration,
        task_identifier: str,
        prompt: str,
        stream_progress: bool = True,
    ):
        self.task_identifier = task_identifier
        self.prompt = prompt
        self._stream_progress = stream_progress
        self._orchestrator = AgentOrchestrator(
            agent_configuration=agent_configuration,
            global_configuration=global_configuration,
        )

    async def run_stream(self, always_yield_text: bool = False) -> AsyncIterator[StreamEvent]:
        """Yield each event as the sub-agent produces it.
        Also pushes events to _agent_event_queues for background monitoring.
        """
        async for event in self._orchestrator.stream(self.prompt):
            queue = _agent_event_queues.get(self.task_identifier)
            if event.type == StreamEvent.Type.TEXT_CHUNK:
                if queue is not None and self._stream_progress:
                    await queue.put(event)
                if self._stream_progress or always_yield_text:
                    yield event
                continue
            if queue is not None:
                await queue.put(event)
            yield event

        queue = _agent_event_queues.get(self.task_identifier)
        if queue is not None:
            await queue.put(None)

    async def run(self) -> str:
        """Collect final text (backwards-compatible for spawn_agent)."""
        last_text = ""
        async for event in self.run_stream():
            if event.type == StreamEvent.Type.TEXT_CHUNK:
                last_text += event.data.get("text", "")
            elif event.type == StreamEvent.Type.DONE:
                last_text = event.data.get("text", last_text)
            elif event.type == StreamEvent.Type.ERROR:
                return event.data.get("message", "unknown")
        if not last_text:
            return json.dumps({"code": "empty_response", "message": "Agent produced no output."})
        return last_text


@dataclass(frozen=True)
class BackgroundKind:
    registry: Any
    active_context_key: str
    completed_event_type: str
    include_result_in_event: bool = False


@dataclass(frozen=True)
class PendingBackgroundMessage:
    tool_call_id: str
    conversation_index: int


@dataclass(frozen=True)
class BackgroundCompletion:
    tool_name: str
    task_identifier: str
    result: str
    pending_message: PendingBackgroundMessage | None = None


BACKGROUND_KINDS: dict[str, BackgroundKind] = {
    "bash": BackgroundKind(
        registry=bash_tasks,
        active_context_key="pending_bash_commands",
        completed_event_type="background_bash_completed",
    ),
    "web_search": BackgroundKind(
        registry=web_tasks,
        active_context_key="pending_web_searches",
        completed_event_type="background_web_search_completed",
    ),
    "agent": BackgroundKind(
        registry=spawned_tasks,
        active_context_key="pending_agents",
        completed_event_type="agent_completed",
        include_result_in_event=True,
    ),
}


class BackgroundTaskManager:
    def __init__(self, record_event: Callable[[str, dict], None]):
        self._record_event = record_event
        self._tracked_ids: dict[str, set[str]] = {
            tool_name: set() for tool_name in BACKGROUND_KINDS
        }
        self._completed_results: dict[str, list[tuple[str, str]]] = {
            tool_name: [] for tool_name in BACKGROUND_KINDS
        }
        self._pending_messages: dict[str, PendingBackgroundMessage] = {}

    def track(self, tool_name: str, task_identifier: str) -> None:
        if not task_identifier:
            return
        self._tracked_ids.setdefault(tool_name, set()).add(task_identifier)
        self._completed_results.setdefault(tool_name, [])

    def bind_model_message(
        self, task_identifier: str, tool_call_id: str, conversation_index: int,
    ) -> None:
        if not task_identifier:
            return
        self._pending_messages[task_identifier] = PendingBackgroundMessage(
            tool_call_id=tool_call_id,
            conversation_index=conversation_index,
        )

    def poll(self):
        for tool_name, kind in BACKGROUND_KINDS.items():
            tracked_ids = self._tracked_ids[tool_name]
            completed = kind.registry.collect_completed(tracked_ids)
            self._completed_results[tool_name].extend(completed)
            for task_identifier, _ in completed:
                tracked_ids.discard(task_identifier)

    def has_results(self) -> bool:
        return any(self._completed_results.values())

    def drain_results(self) -> list[BackgroundCompletion]:
        results = []
        for tool_name, completed in self._completed_results.items():
            for task_identifier, result in completed:
                results.append(BackgroundCompletion(
                    tool_name=tool_name,
                    task_identifier=task_identifier,
                    result=result,
                    pending_message=self._pending_messages.pop(task_identifier, None),
                ))
            completed.clear()
        return results

    def has_pending(self) -> bool:
        return self.active_background_count() > 0

    def active_background_count(self) -> int:
        return sum(
            kind.registry.active_count_for(self._tracked_ids[tool_name])
            for tool_name, kind in BACKGROUND_KINDS.items()
        )

    def active_tasks(self) -> dict[str, list[str]]:
        active = {}
        for tool_name, kind in BACKGROUND_KINDS.items():
            active_ids = kind.registry.list_active(self._tracked_ids[tool_name])
            if active_ids:
                active[kind.active_context_key] = active_ids
        return active

    def record_completed(self, tool_name: str, task_identifier: str, result: str) -> None:
        kind = BACKGROUND_KINDS[tool_name]
        data = {"task_identifier": task_identifier}
        if kind.include_result_in_event:
            data["result"] = result
        self._record_event(kind.completed_event_type, data)


class Task(BaseModel):
    identifier: str = ""
    description: str
    status: str = "pending"
    dependencies: list[str] = []
    result: str = ""


class TaskManager:
    def __init__(self):
        self._tasks: list[Task] = []
        self._next_identifier: int = 1

    def add_tasks(self, task_definitions: list[dict]) -> list[str]:
        created = []
        for definition in task_definitions:
            identifier = f"task-{self._next_identifier}"
            self._next_identifier += 1
            task = Task(
                identifier=identifier,
                description=definition.get("description", ""),
                dependencies=definition.get("dependencies", []),
            )
            self._tasks.append(task)
            created.append(identifier)
        self._recalculate_statuses()
        return created

    def update_tasks(self, updates: list[dict]) -> list[str]:
        updated_ids = []
        for update in updates:
            task_id = update.get("task_id", "")
            status = update.get("status", "")
            result_value = update.get("result", "")
            for task in self._tasks:
                if task.identifier == task_id:
                    task.status = status
                    if result_value:
                        task.result = result_value
                    updated_ids.append(task_id)
                    break
        if updated_ids:
            self._recalculate_statuses()
        return updated_ids

    def _recalculate_statuses(self) -> None:
        for task in self._tasks:
            if task.status == "blocked":
                if all(self._is_dependency_met(dep) for dep in task.dependencies):
                    task.status = "pending"

    def _is_dependency_met(self, dependency_id: str) -> bool:
        for task in self._tasks:
            if task.identifier == dependency_id:
                return task.status == "completed"
        return True

    def render_json(self) -> str:
        if not self._tasks:
            return ""
        return json.dumps([task.model_dump() for task in self._tasks])

    def to_dict_list(self) -> list[dict]:
        return [task.model_dump() for task in self._tasks]


class AgentOrchestrator:
    # Maximum time to block a turn waiting for in-flight background tasks
    # (searches, sub-agents, slow bash) before invoking the model anyway.
    _BACKGROUND_WAIT_SECONDS = 300.0

    def __init__(
        self,
        agent_configuration: AgentConfiguration,
        global_configuration: GlobalConfiguration,
        pending_permissions: Optional[dict[str, asyncio.Future]] = None,
        on_record_event: Optional[callable] = None,
        on_record_message: Optional[callable] = None,
        on_record_orchestration: Optional[callable] = None,
        session_id: str = "",
        conversation: Optional[list] = None,
        working_directory: str = "",
    ):
        self._session_id = session_id
        self._agent_configuration = agent_configuration
        self._global_configuration = global_configuration
        self._pending_permissions = pending_permissions if pending_permissions is not None else {}
        self._on_record_event = on_record_event
        self._on_record_message = on_record_message
        self._on_record_orchestration = on_record_orchestration
        self._working_directory = working_directory or str(Path.home())

        effective_model = agent_configuration.model or global_configuration.api.model

        self._llm = ReasoningChatOpenAI(
            model=effective_model,
            base_url=global_configuration.api.endpoint,
            api_key=global_configuration.api.api_key,
            reasoning_effort=agent_configuration.reasoning_effort,
            temperature=0,
        )

        self._tools = _build_tools(agent_configuration.tools)
        self._bound_llm = self._llm.bind_tools(
            self._tools,
            parallel_tool_calls=True,
        )
        self._permissions = PermissionEvaluator(agent_configuration)
        self._background = BackgroundTaskManager(self._record_event)

        self._conversation: list = conversation if conversation is not None else []
        self._system_prompt = agent_configuration.system_prompt
        self._recursion_depth: int = 0
        self._calls_this_turn: int = 0
        self._abort_event = asyncio.Event()

        prompts_directory = Path(__file__).parent / "prompts"
        self._prompt_loader = PromptLoader(prompts_directory)
        self._cached_system_prompt: str | None = None
        self._task_manager = TaskManager()
        self._execution_history: list[dict] = []
        self._orchestration_graphs: dict[str, Any] = {}
        self._orchestration_configs: dict[str, dict] = {}
        self._bypass_permissions: bool = False

    @property
    def agent_name(self) -> str:
        return self._agent_configuration.name

    @property
    def working_directory(self) -> str:
        return self._working_directory

    def abort(self) -> None:
        self._abort_event.set()

    def set_bypass_permissions(self, bypass: bool) -> None:
        self._bypass_permissions = bypass

    def _evaluate_bash_permission(self, command: str) -> str:
        if self._bypass_permissions:
            return "allow"
        return self._permissions.evaluate_bash_permission(command)

    def _record_event(self, event_type: str, data: dict) -> None:
        record = {"type": event_type, **data}
        self._execution_history.append(record)
        if self._on_record_event:
            self._on_record_event(event_type, data)

    def _record_message(self, role: str, content: str, tool_call_id: str = "") -> None:
        if self._on_record_message:
            self._on_record_message(role, content, tool_call_id)

    def _build_static_system_prompt(self) -> str:
        """Build the static portion of the system prompt (cached across calls)."""
        if self._cached_system_prompt is None:
            available_agents = list_available_agents(
                self._global_configuration.agents_directory
            )
            context_json = json.dumps({
                "working_directory": self._working_directory,
                "available_agents": available_agents,
            })
            self._cached_system_prompt = self._prompt_loader.load("system_prompt", {
                "system_prompt": self._system_prompt,
                "context": context_json,
                "tasks_section": "",
            })
        return self._cached_system_prompt

    def _build_dynamic_context(self) -> str:
        """Build the dynamic context injected at the end of the message list."""
        parts = []
        current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        parts.append(f"Current time: {current_time}")
        tasks_data = self._task_manager.to_dict_list()
        if tasks_data:
            parts.append(json.dumps({"tasks": tasks_data}))
        pending_info = self._background.active_tasks()
        if pending_info:
            parts.append(json.dumps({"background_tasks_in_progress": pending_info}))
        return "\n".join(parts)

    def _background_result_events(self) -> list[StreamEvent]:
        self._background.poll()
        events: list[StreamEvent] = []
        if not self._background.has_results():
            return events

        for completion in self._background.drain_results():
            if (
                completion.pending_message is not None
                and completion.pending_message.conversation_index < len(self._conversation)
            ):
                self._conversation[completion.pending_message.conversation_index] = ToolMessage(
                    content=completion.result,
                    tool_call_id=completion.pending_message.tool_call_id,
                )
            else:
                message = SystemMessage(
                    content=json.dumps({
                        "type": "background_result",
                        "tool": completion.tool_name,
                        "task_identifier": completion.task_identifier,
                        "result": completion.result,
                    }),
                )
                self._conversation.append(message)
            events.append(StreamEvent(
                StreamEvent.Type.TOOL_RESULT,
                name=completion.tool_name,
                result=_maybe_json(completion.result),
                task_id=completion.task_identifier,
            ))
            self._background.record_completed(
                completion.tool_name,
                completion.task_identifier,
                completion.result,
            )
        return events

    def _record_turn(self, user_message: str, tool_calls: list, tool_results: list, final_response: str):
        self._record_message("human", user_message)
        for tool_call_entry in tool_calls:
            self._record_message("tool", json.dumps(tool_call_entry.get("args", {})), tool_call_entry.get("name", ""))
        for tool_result_entry in tool_results:
            self._record_message("tool", str(tool_result_entry.get("result", "")), tool_result_entry.get("name", ""))
        self._record_message("ai", final_response)

    async def stream(
        self, user_message: str
    ) -> AsyncIterator[StreamEvent]:
        self._abort_event.clear()
        self._calls_this_turn = 0

        self._conversation.append(HumanMessage(content=user_message))

        turn_tool_calls_log: list[dict] = []
        turn_tool_results_log: list[dict] = []
        turn_final_response = ""

        while self._calls_this_turn < self._agent_configuration.maximum_iterations:
            if self._abort_event.is_set():
                yield StreamEvent(StreamEvent.Type.DONE, text="", stop_reason="cancelled")
                return

            while self._background.has_pending():
                yield StreamEvent(
                    StreamEvent.Type.STATUS,
                    code="waiting_for_tools",
                    active=self._background.active_tasks(),
                )
                while (
                    self._background.has_pending()
                    and not self._background.has_results()
                    and not self._abort_event.is_set()
                ):
                    await asyncio.sleep(0.05)
                    self._background.poll()
                if self._abort_event.is_set():
                    yield StreamEvent(StreamEvent.Type.DONE, text="", stop_reason="cancelled")
                    return
                for background_event in self._background_result_events():
                    yield background_event

            for background_event in self._background_result_events():
                yield background_event

            messages = (
                [SystemMessage(content=self._build_static_system_prompt())]
                + self._conversation
                + [SystemMessage(content=self._build_dynamic_context())]
            )

            yield StreamEvent(StreamEvent.Type.STATUS, code="thinking")
            accumulated_response = None
            async for chunk in self._bound_llm.astream(messages):
                if self._abort_event.is_set():
                    yield StreamEvent(StreamEvent.Type.DONE, text="", stop_reason="cancelled")
                    return
                if accumulated_response is None:
                    accumulated_response = chunk
                else:
                    accumulated_response += chunk  # type: ignore[operator]
                if chunk.content and self._agent_configuration.stream_agent_progress:
                    yield StreamEvent(
                        StreamEvent.Type.TEXT_CHUNK,
                        text=chunk.content,
                    )
                reasoning_content = chunk.additional_kwargs.get("reasoning_content", "")
                if reasoning_content:
                    yield StreamEvent(
                        StreamEvent.Type.THINKING,
                        text=reasoning_content,
                    )
            response = accumulated_response

            if not response.tool_calls:
                background_events = self._background_result_events()
                if background_events:
                    for background_event in background_events:
                        yield background_event
                    self._calls_this_turn += 1
                    continue

                final_text = response.content or ""
                turn_final_response = final_text
                self._conversation.append(response)
                self._calls_this_turn = 0
                self._record_turn(
                    user_message, turn_tool_calls_log,
                    turn_tool_results_log, turn_final_response,
                )
                yield StreamEvent(StreamEvent.Type.DONE, text=final_text, stop_reason="completed")
                return

            self._conversation.append(response)

            tool_call_results: list[tuple[str, str, str | None]] = []
            denied_commands: list[str] = []

            for tool_call_data in response.tool_calls:
                if self._abort_event.is_set():
                    self._record_turn(user_message, turn_tool_calls_log, turn_tool_results_log, "")
                    yield StreamEvent(StreamEvent.Type.DONE, text="", stop_reason="cancelled")
                    return

                tool_name = tool_call_data["name"]
                tool_arguments = tool_call_data["args"]
                tool_call_identifier = tool_call_data["id"]

                yield StreamEvent(
                    StreamEvent.Type.TOOL_CALL,
                    name=tool_name,
                    arguments=tool_arguments,
                    id=tool_call_identifier,
                )
                turn_tool_calls_log.append({
                    "name": tool_name,
                    "arguments": tool_arguments,
                })

                try:
                    async for event in self._execute_tool(tool_name, tool_arguments, tool_call_identifier):
                        yield event
                        if event.type == StreamEvent.Type.TOOL_RESULT:
                            result_str = event.data.get("result", "")
                            if (
                                isinstance(result_str, dict)
                                and isinstance(result_str.get("code"), str)
                                and result_str["code"].endswith("_started")
                            ):
                                raw_task_identifier = result_str.get("task_identifier")
                                task_identifier = raw_task_identifier if isinstance(raw_task_identifier, str) else None
                                model_result = json.dumps({
                                    "code": "background_task_scheduled",
                                    "task_identifier": task_identifier,
                                })
                                tool_call_results.append((tool_call_identifier, model_result, task_identifier))
                                turn_tool_results_log.append({"name": tool_name, "result": json.dumps(result_str)})
                                continue
                            if isinstance(result_str, dict):
                                result_str = json.dumps(result_str)
                            tool_call_results.append((tool_call_identifier, str(result_str), None))
                            turn_tool_results_log.append({"name": tool_name, "result": str(result_str)})
                        elif event.type == StreamEvent.Type.ERROR:
                            error_message = event.data.get("message", "unknown error")
                            tool_call_results.append((tool_call_identifier, error_message, None))
                            turn_tool_results_log.append({"name": tool_name, "result": error_message})
                        elif event.type == StreamEvent.Type.DENIED_INJECTION:
                            denied_commands.append(event.data.get("command", ""))
                        elif event.type == StreamEvent.Type.TASKS_UPDATED:
                            result_message = event.data.get("result_message", "")
                            tool_call_results.append((tool_call_identifier, result_message, None))
                            turn_tool_results_log.append({"name": tool_name, "result": result_message})
                        elif event.type == StreamEvent.Type.BACKGROUND_STARTED:
                            result_message = event.data.get("result_message", "")
                            raw_task_identifier = event.data.get("task_id")
                            task_identifier = raw_task_identifier if isinstance(raw_task_identifier, str) else None
                            tool_call_results.append((
                                tool_call_identifier,
                                json.dumps({
                                    "code": "background_task_scheduled",
                                    "task_identifier": task_identifier,
                                }),
                                task_identifier,
                            ))
                            turn_tool_results_log.append({"name": tool_name, "result": result_message})
                except Exception as exception:
                    if tool_call_identifier not in {cid for cid, _, _ in tool_call_results}:
                        error_message = f"Internal error processing {tool_name}: {exception}"
                        tool_call_results.append((tool_call_identifier, error_message, None))
                        yield StreamEvent(
                            StreamEvent.Type.ERROR, id=tool_call_identifier, message=error_message, tool=tool_name,
                        )
                        turn_tool_results_log.append({"name": tool_name, "result": error_message})

            for call_identifier, result, background_task_identifier in tool_call_results:
                conversation_index = len(self._conversation)
                self._conversation.append(
                    ToolMessage(content=result, tool_call_id=call_identifier)
                )
                if background_task_identifier:
                    self._background.bind_model_message(
                        background_task_identifier,
                        call_identifier,
                        conversation_index,
                    )

            if denied_commands:
                commands_list = ", ".join(f"'{cmd}'" for cmd in denied_commands)
                denied_message = self._prompt_loader.load("command_denied", {
                    "commands": commands_list,
                })
                self._conversation.append(SystemMessage(content=denied_message))

            self._calls_this_turn += 1

        self._record_turn(
            user_message, turn_tool_calls_log,
            turn_tool_results_log, "",
        )
        yield StreamEvent(StreamEvent.Type.DONE, text="", stop_reason="maximum_iterations")

    def _load_sub_agent(self, name: str) -> AgentConfiguration:
        return load_agent_configuration(
            name,
            self._global_configuration.agents_directory,
        )

    def _validate_tool_call(self, tool_name: str, arguments: dict) -> tuple[str, str] | None:
        if tool_name == "bash":
            risk = arguments.get("risk", "low")
            if risk not in ("low", "medium", "high"):
                return ("invalid_risk", f"risk must be one of 'low', 'medium', 'high', got '{risk}'.")
            read_only = arguments.get("read_only", True)
            if not isinstance(read_only, bool):
                return ("invalid_read_only", "read_only must be a boolean.")
        if tool_name == "orchestrate":
            steps = arguments.get("steps", [])
            if not isinstance(steps, list) or len(steps) == 0:
                return ("invalid_steps", "steps must be a non-empty list.")
            all_step_ids: set[str] = set()
            for step_index, step in enumerate(steps):
                if not isinstance(step, dict):
                    return ("invalid_step", f"step {step_index} must be an object.")
                if "id" not in step or "agent" not in step or "prompt" not in step:
                    return ("invalid_step", f"step {step_index} must have 'id', 'agent', and 'prompt' fields.")
                step_id = step["id"]
                if step_id in all_step_ids:
                    return ("invalid_step", f"duplicate step id '{step_id}'.")
                all_step_ids.add(step_id)
            for step in steps:
                dependency_ids = step.get("depends_on", [])
                if not isinstance(dependency_ids, list):
                    return ("invalid_step", f"step '{step['id']}' depends_on must be a list.")
                for dependency_id in dependency_ids:
                    if not isinstance(dependency_id, str):
                        return ("invalid_step", f"step '{step['id']}' depends_on entries must be strings.")
                    if dependency_id not in all_step_ids:
                        return ("invalid_dependency", f"step '{step['id']}' depends_on references '{dependency_id}' which is not a step in this orchestration. depends_on must reference other step IDs, not task IDs.")
        return None

    async def _execute_tool(
        self, tool_name: str, tool_arguments: dict, tool_call_identifier: str,
    ) -> AsyncIterator[StreamEvent]:
        """Execute a single tool call, yielding events. The caller collects results from
        TOOL_RESULT, ERROR, TASKS_UPDATED, and BACKGROUND_STARTED events."""

        try:
            self._permissions.check_tool(tool_name, **tool_arguments)
        except PermissionError as exception:
            yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message=str(exception), tool=tool_name)
            return

        validation_error = self._validate_tool_call(tool_name, tool_arguments)
        if validation_error:
            error_code, error_message = validation_error
            yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, code=error_code, message=error_message, tool=tool_name)
            return

        if tool_name == "bash":
            import shlex
            raw_command = tool_arguments.get("command", "")
            directory = self._working_directory
            if directory:
                directory_path = Path(directory).expanduser()
                if not directory_path.is_absolute():
                    yield StreamEvent(
                        StreamEvent.Type.ERROR,
                        id=tool_call_identifier,
                        code="invalid_working_directory",
                        message=f"Working directory must be an absolute path: {directory}",
                        tool=tool_name,
                    )
                    return
                if not directory_path.is_dir():
                    yield StreamEvent(
                        StreamEvent.Type.ERROR,
                        id=tool_call_identifier,
                        code="invalid_working_directory",
                        message=f"Working directory does not exist: {directory}",
                        tool=tool_name,
                    )
                    return
                tool_arguments = dict(tool_arguments)
                tool_arguments["command"] = f"cd {shlex.quote(str(directory_path))} && {raw_command}"
            command = tool_arguments.get("command", "")
            justification = tool_arguments.get("justification", "")
            risk = tool_arguments.get("risk", "")
            read_only = tool_arguments.get("read_only", True)
            if isinstance(read_only, str):
                read_only = read_only.lower() == "true"

            permission_decision = self._evaluate_bash_permission(command)
            if permission_decision == "deny":
                deny_message = f"Command '{command}' is not permitted."
                yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message=deny_message, tool=tool_name)
                yield StreamEvent(
                    StreamEvent.Type.DENIED_INJECTION,
                    id=tool_call_identifier,
                    command=command,
                )
                return
            elif not read_only and (permission_decision == "ask" or risk in ("medium", "high")):
                request_identifier = f"perm-{self._session_id[:8]}-{uuid.uuid4().hex[:12]}"
                future = asyncio.get_event_loop().create_future()
                self._pending_permissions[request_identifier] = future
                yield StreamEvent(
                    StreamEvent.Type.PERMISSION_REQUEST,
                    id=tool_call_identifier,
                    request_id=request_identifier,
                    command=command,
                    justification=justification,
                    risk=risk,
                )
                try:
                    allowed = await future
                finally:
                    self._pending_permissions.pop(request_identifier, None)
                if not allowed:
                    yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message="command not approved by user", tool=tool_name)
                    return

            result = await bash_tool.ainvoke(tool_arguments)
            result_data = _maybe_json(result)
            yield StreamEvent(StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=result_data)
            if isinstance(result_data, dict) and result_data.get("code") == "bash_started":
                task_identifier = result_data.get("task_identifier", "")
                if task_identifier:
                    self._background.track("bash", task_identifier)
                    self._record_event("background_bash_started", {"task_identifier": task_identifier, "command": command[:200]})

        elif tool_name == "spawn_agent":
            self._recursion_depth += 1
            try:
                self._permissions.check_spawn_agent(self._recursion_depth, self._background.active_background_count())
            except PermissionError as exception:
                self._recursion_depth -= 1
                yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message=str(exception), tool=tool_name)
                return

            sub_agent_prompt = tool_arguments.get("prompt", "")
            sub_agent_name = tool_arguments.get("agent", "main")
            sub_agent_task_identifier = f"agent-{uuid.uuid4().hex[:12]}"

            try:
                sub_configuration = self._load_sub_agent(sub_agent_name)
            except FileNotFoundError as exception:
                self._recursion_depth -= 1
                yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message=str(exception), tool=tool_name)
                return

            runner = SubAgentRunner(
                agent_configuration=sub_configuration,
                global_configuration=self._global_configuration,
                task_identifier=sub_agent_task_identifier,
                prompt=sub_agent_prompt,
                stream_progress=self._agent_configuration.stream_agent_progress,
            )

            async def _run_and_cleanup(runner=runner, tid=sub_agent_task_identifier):
                try:
                    return await runner.run()
                finally:
                    self._recursion_depth = max(0, self._recursion_depth - 1)
                    _agent_event_queues.pop(tid, None)

            agent_queue: asyncio.Queue[Optional[StreamEvent]] = asyncio.Queue()
            _agent_event_queues[sub_agent_task_identifier] = agent_queue
            register_spawned_task(sub_agent_task_identifier, _run_and_cleanup())
            self._background.track("agent", sub_agent_task_identifier)
            self._record_event("agent_spawned", {"task_identifier": sub_agent_task_identifier, "agent": sub_agent_name, "prompt": sub_agent_prompt[:200]})

            result_message = f"Started sub-agent ({sub_agent_task_identifier}) using profile '{sub_agent_name}'."
            yield StreamEvent(
                StreamEvent.Type.BACKGROUND_STARTED,
                id=tool_call_identifier,
                task_id=sub_agent_task_identifier,
                agent=sub_agent_name,
                result_message=result_message,
            )

        elif tool_name == "orchestrate":
            steps = tool_arguments.get("steps", [])
            if not steps:
                yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message="orchestrate requires at least one step.", tool=tool_name)
                return

            orchestration_id = f"orch-{uuid.uuid4().hex[:12]}"
            config = {"configurable": {"thread_id": orchestration_id}}

            graph = compile_orchestration_graph(steps=steps, node_factory=self._make_orchestration_node)
            self._orchestration_graphs[orchestration_id] = graph
            self._orchestration_configs[orchestration_id] = config

            # Announce the orchestration so the UI can group all of its sub-agent
            # activity into a dedicated panel and link it back to this tool call.
            yield StreamEvent(
                StreamEvent.Type.ORCHESTRATION_STARTED,
                orchestration_id=orchestration_id,
                tool_call_id=tool_call_identifier,
                justification=tool_arguments.get("justification", ""),
                steps=[{"id": step["id"], "agent": step["agent"]} for step in steps],
            )

            initial_state: OrchestrationState = {"steps": steps, "results": []}
            orchestration_results: list[dict] = []

            async for mode, data in graph.astream(initial_state, config, stream_mode=["updates", "custom"]):
                if mode == "custom":
                    custom_type = data.pop("type", None)
                    # Every sub-agent event carries its orchestration so the UI
                    # can route it to the right group/step without ambiguity
                    # when several orchestrations run in one session.
                    data["orchestration_id"] = orchestration_id
                    if custom_type == "agent_text_chunk":
                        # Only incremental chunks are displayed. The final
                        # ``agent_result`` carries the full text again and must
                        # not be re-streamed, or each step would render twice.
                        yield StreamEvent(StreamEvent.Type.AGENT_TEXT_CHUNK, **data)
                    elif custom_type == "agent_tool_call":
                        yield StreamEvent(StreamEvent.Type.AGENT_TOOL_CALL, **data)
                    elif custom_type == "agent_thinking":
                        yield StreamEvent(StreamEvent.Type.AGENT_THINKING, **data)
                    elif custom_type == "agent_status":
                        yield StreamEvent(StreamEvent.Type.AGENT_STATUS, **data)
                elif mode == "updates":
                    for node_name, state_update in data.items():
                        if "results" in state_update:
                            new_results = state_update["results"]
                            orchestration_results.extend(new_results)
                            result_id = new_results[-1]["id"]
                            yield StreamEvent(
                                StreamEvent.Type.AGENT_DONE,
                                agent_id=result_id,
                                step_id=result_id,
                                orchestration_id=orchestration_id,
                            )

            self._record_event("orchestration", {
                "orchestration_id": orchestration_id, "thread_id": orchestration_id,
                "steps": [{"id": step["id"], "agent": step["agent"]} for step in steps],
                "results": orchestration_results,
            })
            if self._on_record_orchestration:
                self._on_record_orchestration(
                    orchestration_id=orchestration_id, thread_id=orchestration_id,
                    steps=[{"id": step["id"], "agent": step["agent"]} for step in steps],
                    results=orchestration_results,
                )

            result = json.dumps({"code": "orchestration_completed", "results": orchestration_results})
            yield StreamEvent(StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=_maybe_json(result))

        elif tool_name == "write_tasks":
            task_definitions = tool_arguments.get("tasks", [])
            identifiers = self._task_manager.add_tasks(task_definitions)
            result_message = f"Created tasks: {', '.join(identifiers)}"
            yield StreamEvent(
                StreamEvent.Type.TASKS_UPDATED,
                id=tool_call_identifier,
                tasks=self._task_manager.to_dict_list(),
                result_message=result_message,
            )

        elif tool_name == "update_tasks":
            updates = tool_arguments.get("updates", [])
            updated_ids = self._task_manager.update_tasks(updates)
            if updated_ids:
                result_message = f"Updated tasks: {', '.join(updated_ids)}"
            else:
                result_message = "No matching tasks found."
            updated_tasks = [
                t for t in self._task_manager.to_dict_list()
                if t["identifier"] in updated_ids
            ]
            yield StreamEvent(
                StreamEvent.Type.TASKS_UPDATED,
                id=tool_call_identifier,
                tasks=updated_tasks,
                result_message=result_message,
            )

        elif tool_name == "web_search":
            result = await web_search_tool.ainvoke(tool_arguments)
            result_data = _maybe_json(result)
            yield StreamEvent(StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=result_data)
            if isinstance(result_data, dict) and result_data.get("code") == "web_search_started":
                task_identifier = result_data.get("task_identifier", "")
                if task_identifier:
                    self._background.track("web_search", task_identifier)

        else:
            yield StreamEvent(
                StreamEvent.Type.ERROR, id=tool_call_identifier,
                message=f"Unknown tool: {tool_name}", tool=tool_name,
            )

    def get_execution_history(self) -> list[dict]:
        return self._execution_history

    def get_orchestration_checkpoints(self, thread_id: str) -> list[dict]:
        graph = self._orchestration_graphs.get(thread_id)
        config = self._orchestration_configs.get(thread_id)
        if not graph or not config:
            return []
        snapshots = list(graph.get_state_history(config))
        return [
            {
                "step": index,
                "state": snapshot.values,
            }
            for index, snapshot in enumerate(snapshots)
        ]

    def _make_orchestration_node(self, step: dict, node_index: int):
        async def node(state: OrchestrationState) -> OrchestrationState:
            from langgraph.config import get_stream_writer
            writer = get_stream_writer()

            agent_configuration = self._load_sub_agent(step["agent"])

            if "depends_on" not in step:
                dependency_ids = [state["steps"][node_index - 1]["id"]] if node_index > 0 else []
            else:
                dependency_ids = step["depends_on"]

            dependency_outputs = [
                result for result in state["results"]
                if result["id"] in dependency_ids
            ]

            resolved_prompt = step["prompt"]
            if dependency_outputs:
                resolved_prompt += "\n\n" + json.dumps(dependency_outputs)

            runner = SubAgentRunner(
                agent_configuration=agent_configuration,
                global_configuration=self._global_configuration,
                task_identifier=f"orch-{step['id']}",
                prompt=resolved_prompt,
                stream_progress=self._agent_configuration.stream_agent_progress,
            )

            step_text = ""
            result = ""
            async for event in runner.run_stream(always_yield_text=True):
                if event.type == StreamEvent.Type.TEXT_CHUNK:
                    chunk_text = event.data.get("text", "")
                    step_text += chunk_text
                    writer({"type": "agent_text_chunk", "step_id": step["id"], "text": chunk_text})
                elif event.type == StreamEvent.Type.THINKING:
                    writer({"type": "agent_thinking", "step_id": step["id"], "text": event.data.get("text", "")})
                elif event.type == StreamEvent.Type.TOOL_CALL:
                    writer({"type": "agent_tool_call", "step_id": step["id"], "name": event.data.get("name", ""), "arguments": event.data.get("arguments", {})})
                elif event.type == StreamEvent.Type.STATUS:
                    writer({"type": "agent_status", "step_id": step["id"], "code": event.data.get("code", "")})
                elif event.type == StreamEvent.Type.DONE:
                    # Captured for the orchestration's structured result only;
                    # the incremental chunks already streamed it to the user.
                    result = event.data.get("text", step_text)
                elif event.type == StreamEvent.Type.ERROR:
                    result = event.data.get("message", "unknown")

            return {"results": [{"id": step["id"], "agent": step["agent"], "output": result}]}
        return node
