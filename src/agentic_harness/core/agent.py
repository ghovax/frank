import asyncio
import json
import uuid
from enum import Enum
from typing import Any, AsyncIterator, Optional

from langchain_openai import ChatOpenAI
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool

from agentic_harness.core.agent_configuration import (
    AgentConfiguration,
    ToolsConfiguration,
)
from agentic_harness.core.configuration import GlobalConfiguration
from agentic_harness.core.permissions import PermissionEvaluator, PermissionError
from agentic_harness.tools.bash import bash as bash_tool, collect_background_bash_results
from agentic_harness.tools.read import read as read_tool
from agentic_harness.tools.edit import edit as edit_tool
from agentic_harness.tools.agent_spawn import spawn_agent as spawn_tool, register_spawned_task, collect_completed_agents


class StreamEvent:
    class Type(str, Enum):
        TEXT_CHUNK = "text_chunk"
        TOOL_CALL = "tool_call"
        TOOL_RESULT = "tool_result"
        DONE = "done"
        BACKGROUND_STARTED = "background_started"
        ERROR = "error"
        STATUS = "status"

    def __init__(self, type: Type, **data):
        self.type = type
        self.data = data

    def to_dict(self) -> dict:
        return {"type": self.type.value, **self.data}

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


def _build_tools(tools_config: ToolsConfiguration) -> list[BaseTool]:
    available = []
    if tools_config.bash.enabled:
        available.append(bash_tool)
    if tools_config.read.enabled:
        available.append(read_tool)
    if tools_config.edit.enabled:
        available.append(edit_tool)
    if tools_config.spawn_agent.enabled:
        available.append(spawn_tool)
    return available


class SubAgentRunner:
    def __init__(
        self,
        agent_config: AgentConfiguration,
        global_config: GlobalConfiguration,
        task_id: str,
        prompt: str,
    ):
        self.task_id = task_id
        self.prompt = prompt
        self._orchestrator = AgentOrchestrator(
            agent_config=agent_config,
            global_config=global_config,
        )

    async def run(self) -> str:
        last_text = ""
        async for event in self._orchestrator.stream(self.prompt):
            if event.type == StreamEvent.Type.TEXT_CHUNK:
                last_text += event.data.get("text", "")
            elif event.type == StreamEvent.Type.DONE:
                last_text = event.data.get("text", last_text)
            elif event.type == StreamEvent.Type.ERROR:
                return f"[error: {event.data.get('message', 'unknown')}]"
        return last_text or "(no response)"


class BackgroundTaskManager:
    def __init__(self):
        self._bash_results: list[tuple[str, str]] = []
        self._agent_results: list[tuple[str, str]] = []
        self._active_agent_tasks: dict[str, asyncio.Task] = {}

    def poll(self):
        self._bash_results = collect_background_bash_results()
        self._agent_results = collect_completed_agents()

    def has_results(self) -> bool:
        return bool(self._bash_results) or bool(self._agent_results)

    def drain_results(self) -> list[tuple[str, str, str]]:
        results = []
        for task_id, result in self._bash_results:
            results.append(("bash", task_id, result))
        for task_id, result in self._agent_results:
            results.append(("agent", task_id, result))
        self._bash_results = []
        self._agent_results = []
        return results

    def active_agent_count(self) -> int:
        return len(collect_completed_agents()) + len(collect_background_bash_results())


class AgentOrchestrator:
    def __init__(
        self,
        agent_config: AgentConfiguration,
        global_config: GlobalConfiguration,
    ):
        self._agent_config = agent_config
        self._global_config = global_config

        effective_model = agent_config.model or global_config.api.model

        self._llm = ChatOpenAI(
            model=effective_model,
            base_url=global_config.api.endpoint,
            api_key=global_config.api.api_key,
            reasoning_effort=agent_config.reasoning_effort,
            temperature=0,
        )

        self._tools = _build_tools(agent_config.tools)
        self._bound_llm = self._llm.bind_tools(
            self._tools,
            parallel_tool_calls=True,
        )
        self._permissions = PermissionEvaluator(agent_config)
        self._background = BackgroundTaskManager()

        self._conversation: list = []
        self._system_prompt = agent_config.system_prompt
        self._recursion_depth: int = 0
        self._calls_this_turn: int = 0

    @property
    def agent_name(self) -> str:
        return self._agent_config.name

    def _build_system_prompt(self) -> str:
        import os
        cwd = os.getcwd()
        return (
            self._system_prompt
            + f"\n\n[Context]\nCurrent working directory: {cwd}\n"
            f"Available agent profiles: main, explore, code\n"
            f"Use absolute paths for file operations."
        )

    async def stream(self, user_message: str) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(StreamEvent.Type.STATUS, message="processing")

        self._conversation.append(HumanMessage(content=user_message))

        while self._calls_this_turn < self._agent_config.maximum_iterations:
            background_injected = False

            self._background.poll()
            if self._background.has_results():
                for tool_name, task_id, result in self._background.drain_results():
                    msg = ToolMessage(
                        content=f"[background {tool_name} {task_id} completed]\n{result}",
                        tool_call_id=f"bg-{task_id}",
                    )
                    self._conversation.append(msg)
                    yield StreamEvent(StreamEvent.Type.TOOL_RESULT, name=tool_name, result=result, task_id=task_id)
                background_injected = True

            messages = (
                [SystemMessage(content=self._build_system_prompt())]
                + self._conversation
            )

            response = await self._bound_llm.ainvoke(messages)

            if not response.tool_calls:
                yield StreamEvent(StreamEvent.Type.DONE, text=response.content or "")
                self._conversation.append(response)
                self._calls_this_turn = 0
                return

            self._conversation.append(response)

            tool_call_results: list[tuple[str, str]] = []

            for tool_call in response.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                tool_call_id = tool_call["id"]

                yield StreamEvent(
                    StreamEvent.Type.TOOL_CALL,
                    name=tool_name,
                    args=tool_args,
                    id=tool_call_id,
                )

                try:
                    self._permissions.check_tool(tool_name, **tool_args)
                except PermissionError as e:
                    error_msg = str(e)
                    tool_call_results.append((tool_call_id, error_msg))
                    yield StreamEvent(
                        StreamEvent.Type.ERROR,
                        message=error_msg,
                        tool=tool_name,
                    )
                    continue

                if tool_name == "bash":
                    result = bash_tool.invoke(tool_args)
                    tool_call_results.append((tool_call_id, result))
                    yield StreamEvent(StreamEvent.Type.TOOL_RESULT, name=tool_name, result=result)

                elif tool_name == "read":
                    result = read_tool.invoke(tool_args)
                    tool_call_results.append((tool_call_id, result))
                    yield StreamEvent(StreamEvent.Type.TOOL_RESULT, name=tool_name, result=result)

                elif tool_name == "edit":
                    result = edit_tool.invoke(tool_args)
                    tool_call_results.append((tool_call_id, result))
                    yield StreamEvent(StreamEvent.Type.TOOL_RESULT, name=tool_name, result=result)

                elif tool_name == "spawn_agent":
                    self._recursion_depth += 1
                    try:
                        self._permissions.check_spawn_agent(
                            self._recursion_depth,
                            self._background.active_agent_count(),
                        )
                    except PermissionError as e:
                        self._recursion_depth -= 1
                        error_msg = str(e)
                        tool_call_results.append((tool_call_id, error_msg))
                        yield StreamEvent(StreamEvent.Type.ERROR, message=error_msg, tool=tool_name)
                        continue

                    sub_prompt = tool_args.get("prompt", "")
                    sub_agent_name = tool_args.get("agent", "main")

                    sub_task_id = f"agent-{uuid.uuid4().hex[:12]}"
                    try:
                        sub_config = self._load_sub_agent(sub_agent_name)
                    except FileNotFoundError as e:
                        self._recursion_depth -= 1
                        error_msg = str(e)
                        tool_call_results.append((tool_call_id, error_msg))
                        yield StreamEvent(StreamEvent.Type.ERROR, message=error_msg, tool=tool_name)
                        continue

                    runner = SubAgentRunner(
                        agent_config=sub_config,
                        global_config=self._global_config,
                        task_id=sub_task_id,
                        prompt=sub_prompt,
                    )

                    task = asyncio.create_task(runner.run())
                    register_spawned_task(sub_task_id, runner.run())

                    result_msg = f"[sub-agent {sub_task_id} started with agent '{sub_agent_name}']"
                    tool_call_results.append((tool_call_id, result_msg))
                    yield StreamEvent(
                        StreamEvent.Type.BACKGROUND_STARTED,
                        task_id=sub_task_id,
                        agent=sub_agent_name,
                    )

            for call_id, result in tool_call_results:
                self._conversation.append(
                    ToolMessage(content=result, tool_call_id=call_id)
                )

            self._calls_this_turn += 1

        yield StreamEvent(
            StreamEvent.Type.DONE,
            text="[reached maximum iterations without final response]",
        )

    def _load_sub_agent(self, name: str) -> AgentConfiguration:
        from agentic_harness.core.agent_configuration import load_agent_configuration
        return load_agent_configuration(
            name,
            self._global_config.agents_directory,
        )
