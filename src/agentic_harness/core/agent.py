import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from langchain_openai import ChatOpenAI
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from agentic_harness.core.configuration import (
    AgentConfiguration,
    GlobalConfiguration,
    PermissionEvaluator,
    PermissionError,
    PromptLoader,
    load_agent_configuration,
    list_available_agents,
)
from agentic_harness.tools.tools import (
    bash as bash_tool,
    read as read_tool,
    edit as edit_tool,
    spawn_agent as spawn_tool,
    write_tasks as write_tasks_tool,
    update_task as update_task_tool,
    register_spawned_task,
    collect_background_bash_results,
    collect_completed_agents,
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

    def __init__(self, event_type: Type, **data):
        self.type = event_type
        self.data = data

    def to_dict(self) -> dict:
        return {"type": self.type.value, **self.data}

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


def _build_tools(tools_configuration) -> list[BaseTool]:
    available = []
    if tools_configuration.bash.enabled:
        available.append(bash_tool)
    if tools_configuration.read.enabled:
        available.append(read_tool)
    if tools_configuration.edit.enabled:
        available.append(edit_tool)
    if tools_configuration.spawn_agent.enabled:
        available.append(spawn_tool)
    available.append(write_tasks_tool)
    available.append(update_task_tool)
    return available


class SubAgentRunner:
    def __init__(
        self,
        agent_configuration: AgentConfiguration,
        global_configuration: GlobalConfiguration,
        task_identifier: str,
        prompt: str,
    ):
        self.task_identifier = task_identifier
        self.prompt = prompt
        self._orchestrator = AgentOrchestrator(
            agent_configuration=agent_configuration,
            global_configuration=global_configuration,
        )

    async def run(self) -> str:
        last_text = ""
        async for event in self._orchestrator.stream(self.prompt):
            if event.type == StreamEvent.Type.TEXT_CHUNK:
                last_text += event.data.get("text", "")
            elif event.type == StreamEvent.Type.DONE:
                last_text = event.data.get("text", last_text)
            elif event.type == StreamEvent.Type.ERROR:
                return f"Error: {event.data.get('message', 'unknown')}"
        return last_text or "No response."


class BackgroundTaskManager:
    def __init__(self):
        self._bash_results: list[tuple[str, str]] = []
        self._agent_results: list[tuple[str, str]] = []

    def poll(self):
        self._bash_results = collect_background_bash_results()
        self._agent_results = collect_completed_agents()

    def has_results(self) -> bool:
        return bool(self._bash_results) or bool(self._agent_results)

    def drain_results(self) -> list[tuple[str, str, str]]:
        results = []
        for task_identifier, result in self._bash_results:
            results.append(("bash", task_identifier, result))
        for task_identifier, result in self._agent_results:
            results.append(("agent", task_identifier, result))
        self._bash_results = []
        self._agent_results = []
        return results

    def active_background_count(self) -> int:
        return len(collect_background_bash_results()) + len(collect_completed_agents())


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

    def update_task(self, task_id: str, status: str, result: str = "") -> bool:
        for task in self._tasks:
            if task.identifier == task_id:
                task.status = status
                if result:
                    task.result = result
                self._recalculate_statuses()
                return True
        return False

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
    def __init__(
        self,
        agent_configuration: AgentConfiguration,
        global_configuration: GlobalConfiguration,
        pending_permissions: Optional[dict[str, asyncio.Future]] = None,
    ):
        self._agent_configuration = agent_configuration
        self._global_configuration = global_configuration
        self._pending_permissions = pending_permissions or {}

        effective_model = agent_configuration.model or global_configuration.api.model

        self._llm = ChatOpenAI(
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
        self._background = BackgroundTaskManager()

        self._conversation: list = []
        self._system_prompt = agent_configuration.system_prompt
        self._recursion_depth: int = 0
        self._calls_this_turn: int = 0

        prompts_directory = Path(__file__).parent / "prompts"
        self._prompt_loader = PromptLoader(prompts_directory)
        self._session_history: list[dict] = []
        self._task_manager = TaskManager()

    @property
    def agent_name(self) -> str:
        return self._agent_configuration.name

    def _build_system_prompt(self) -> str:
        current_working_directory = os.getcwd()
        available_agents = ", ".join(
            list_available_agents(self._global_configuration.agents_directory)
        )
        tasks_json = self._task_manager.render_json()
        return self._prompt_loader.load("system_prompt", {
            "SYSTEM_PROMPT": self._system_prompt,
            "CONTEXT": self._prompt_loader.load("context", {
                "current_working_directory": current_working_directory,
                "available_agents": available_agents,
            }),
            "TASKS_SECTION": self._prompt_loader.load("task_section", {"TASKS_JSON": tasks_json}) if tasks_json else "",
            "TASK_INSTRUCTION": self._prompt_loader.load("task_instruction", {}),
        })

    def _record_turn(self, user_message: str, tool_calls: list, tool_results: list, final_response: str):
        history_directory = Path("history")
        history_directory.mkdir(exist_ok=True)
        history_file = history_directory / f"{self.agent_name}.jsonl"
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": self.agent_name,
            "user_message": user_message,
            "tool_calls": [
                {"name": tool_call_entry.get("name", ""), "arguments": tool_call_entry.get("args", {})}
                for tool_call_entry in tool_calls
            ],
            "tool_results": [
                {"name": tr.get("name", ""), "result": tr.get("result", "")}
                for tr in tool_results
            ],
            "final_response": final_response,
        }
        with open(history_file, "a") as file_handle:
            file_handle.write(json.dumps(record) + "\n")

    async def stream(
        self, user_message: str
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(StreamEvent.Type.STATUS, message="Sending message...")

        self._conversation.append(HumanMessage(content=user_message))

        turn_tool_calls_log: list[dict] = []
        turn_tool_results_log: list[dict] = []
        turn_final_response = ""

        while self._calls_this_turn < self._agent_configuration.maximum_iterations:
            self._background.poll()
            if self._background.has_results():
                for tool_name, task_identifier, result in self._background.drain_results():
                    message = ToolMessage(
                        content=f"Background {tool_name} ({task_identifier}) completed.\n{result}",
                        tool_call_id=f"bg-{task_identifier}",
                    )
                    self._conversation.append(message)
                    yield StreamEvent(
                        StreamEvent.Type.TOOL_RESULT,
                        name=tool_name,
                        result=result,
                        task_id=task_identifier,
                    )

            messages = (
                [SystemMessage(content=self._build_system_prompt())]
                + self._conversation
            )

            accumulated_response = None
            async for chunk in self._bound_llm.astream(messages):
                if accumulated_response is None:
                    accumulated_response = chunk
                else:
                    accumulated_response += chunk  # type: ignore[operator]
                if chunk.content:
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
                final_text = response.content or ""
                turn_final_response = final_text
                yield StreamEvent(StreamEvent.Type.DONE, text=final_text)
                self._conversation.append(response)
                self._calls_this_turn = 0
                self._record_turn(
                    user_message, turn_tool_calls_log,
                    turn_tool_results_log, turn_final_response,
                )
                return

            self._conversation.append(response)

            tool_call_results: list[tuple[str, str]] = []

            for tool_call_data in response.tool_calls:
                tool_name = tool_call_data["name"]
                tool_arguments = tool_call_data["args"]
                tool_call_identifier = tool_call_data["id"]

                yield StreamEvent(
                    StreamEvent.Type.TOOL_CALL,
                    name=tool_name,
                    args=tool_arguments,
                    id=tool_call_identifier,
                )
                turn_tool_calls_log.append({
                    "name": tool_name,
                    "args": tool_arguments,
                })

                try:
                    self._permissions.check_tool(tool_name, **tool_arguments)
                except PermissionError as exception:
                    error_message = str(exception)
                    tool_call_results.append((tool_call_identifier, error_message))
                    yield StreamEvent(
                        StreamEvent.Type.ERROR,
                        message=error_message,
                        tool=tool_name,
                    )
                    turn_tool_results_log.append({
                        "name": tool_name,
                        "result": error_message,
                    })
                    continue

                if tool_name == "bash":
                    command = tool_arguments.get("command", "")
                    justification = tool_arguments.get("justification", "")
                    risk = tool_arguments.get("risk", "")
                    background = tool_arguments.get("background", False)

                    permission_decision = self._permissions.evaluate_bash_permission(command)
                    if permission_decision == "deny":
                        error_message = (
                            f"Command '{command[:100]}' is not permitted."
                        )
                        tool_call_results.append((tool_call_identifier, error_message))
                        yield StreamEvent(StreamEvent.Type.ERROR, message=error_message, tool=tool_name)
                        turn_tool_results_log.append({"name": tool_name, "result": error_message})
                        continue
                    elif permission_decision == "ask":
                        request_identifier = f"perm-{uuid.uuid4().hex[:12]}"
                        future = asyncio.get_event_loop().create_future()
                        self._pending_permissions[request_identifier] = future
                        yield StreamEvent(
                            StreamEvent.Type.PERMISSION_REQUEST,
                            request_id=request_identifier,
                            command=command,
                            justification=justification,
                            risk=risk,
                        )
                        try:
                            allowed = await asyncio.wait_for(future, timeout=60)
                        except asyncio.TimeoutError:
                            allowed = False
                        finally:
                            self._pending_permissions.pop(request_identifier, None)
                        if not allowed:
                            error_message = "Command not approved by user."
                            tool_call_results.append((tool_call_identifier, error_message))
                            yield StreamEvent(StreamEvent.Type.ERROR, message=error_message, tool=tool_name)
                            turn_tool_results_log.append({"name": tool_name, "result": error_message})
                            continue

                    result = bash_tool.invoke(tool_arguments)
                    tool_call_results.append((tool_call_identifier, result))
                    yield StreamEvent(
                        StreamEvent.Type.TOOL_RESULT, name=tool_name, result=result
                    )
                    turn_tool_results_log.append({"name": tool_name, "result": result})

                elif tool_name == "read":
                    result = read_tool.invoke(tool_arguments)
                    tool_call_results.append((tool_call_identifier, result))
                    yield StreamEvent(
                        StreamEvent.Type.TOOL_RESULT, name=tool_name, result=result
                    )
                    turn_tool_results_log.append({"name": tool_name, "result": result})

                elif tool_name == "edit":
                    result = edit_tool.invoke(tool_arguments)
                    tool_call_results.append((tool_call_identifier, result))
                    yield StreamEvent(
                        StreamEvent.Type.TOOL_RESULT, name=tool_name, result=result
                    )
                    turn_tool_results_log.append({"name": tool_name, "result": result})

                elif tool_name == "spawn_agent":
                    self._recursion_depth += 1
                    try:
                        self._permissions.check_spawn_agent(
                            self._recursion_depth,
                            self._background.active_background_count(),
                        )
                    except PermissionError as exception:
                        self._recursion_depth -= 1
                        error_message = str(exception)
                        tool_call_results.append((tool_call_identifier, error_message))
                        yield StreamEvent(
                            StreamEvent.Type.ERROR, message=error_message, tool=tool_name
                        )
                        turn_tool_results_log.append({"name": tool_name, "result": error_message})
                        continue

                    sub_agent_prompt = tool_arguments.get("prompt", "")
                    sub_agent_name = tool_arguments.get("agent", "main")
                    sub_agent_task_identifier = f"agent-{uuid.uuid4().hex[:12]}"

                    try:
                        sub_configuration = self._load_sub_agent(sub_agent_name)
                    except FileNotFoundError as exception:
                        self._recursion_depth -= 1
                        error_message = str(exception)
                        tool_call_results.append((tool_call_identifier, error_message))
                        yield StreamEvent(
                            StreamEvent.Type.ERROR, message=error_message, tool=tool_name
                        )
                        turn_tool_results_log.append({"name": tool_name, "result": error_message})
                        continue

                    runner = SubAgentRunner(
                        agent_configuration=sub_configuration,
                        global_configuration=self._global_configuration,
                        task_identifier=sub_agent_task_identifier,
                        prompt=sub_agent_prompt,
                    )

                    register_spawned_task(sub_agent_task_identifier, runner.run())

                    result_message = (
                        f"Started sub-agent ({sub_agent_task_identifier}) "
                        f"using profile '{sub_agent_name}'."
                    )
                    tool_call_results.append((tool_call_identifier, result_message))
                    yield StreamEvent(
                        StreamEvent.Type.BACKGROUND_STARTED,
                        task_id=sub_agent_task_identifier,
                        agent=sub_agent_name,
                    )
                    turn_tool_results_log.append({
                        "name": tool_name,
                        "result": result_message,
                    })

                elif tool_name == "write_tasks":
                    task_definitions = tool_arguments.get("tasks", [])
                    identifiers = self._task_manager.add_tasks(task_definitions)
                    result_message = f"Created tasks: {', '.join(identifiers)}"
                    tool_call_results.append((tool_call_identifier, result_message))
                    yield StreamEvent(
                        StreamEvent.Type.TASKS_UPDATED,
                        tasks=self._task_manager.to_dict_list(),
                    )
                    turn_tool_results_log.append({
                        "name": tool_name,
                        "result": result_message,
                    })

                elif tool_name == "update_task":
                    task_id = tool_arguments.get("task_id", "")
                    status = tool_arguments.get("status", "")
                    result_value = tool_arguments.get("result", "")
                    success = self._task_manager.update_task(task_id, status, result_value)
                    if success:
                        result_message = f"Updated task {task_id} to {status}."
                    else:
                        result_message = f"Task {task_id} not found."
                    tool_call_results.append((tool_call_identifier, result_message))
                    yield StreamEvent(
                        StreamEvent.Type.TASKS_UPDATED,
                        tasks=self._task_manager.to_dict_list(),
                    )
                    turn_tool_results_log.append({
                        "name": tool_name,
                        "result": result_message,
                    })

            for call_identifier, result in tool_call_results:
                self._conversation.append(
                    ToolMessage(content=result, tool_call_id=call_identifier)
                )

            self._calls_this_turn += 1

        final_text = "Stopped: reached maximum iterations without a final response."
        turn_final_response = final_text
        yield StreamEvent(StreamEvent.Type.DONE, text=final_text)
        self._record_turn(
            user_message, turn_tool_calls_log,
            turn_tool_results_log, turn_final_response,
        )

    def _load_sub_agent(self, name: str) -> AgentConfiguration:
        return load_agent_configuration(
            name,
            self._global_configuration.agents_directory,
        )
