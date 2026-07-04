import asyncio
import json
import os
import platform
import shlex
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Literal, Optional

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.ai import add_ai_message_chunks
from langchain_core.tools import BaseTool
from pydantic import BaseModel, SecretStr

from a2a.types import Task, TaskState


from harness.core.configuration import (
    AgentConfiguration,
    GlobalConfiguration,
    PermissionEvaluator,
    PermissionError,
    PromptLoader,
    load_agent_configuration,
    describe_available_agents,
)
from harness.core.litellm_model import ChatLiteLLMModel
from harness.core.file_leases import FileLeaseConflict, FileLeaseManager
from harness.core.models import resolve_litellm
from harness.tools.tools import (
    bash as bash_tool,
    web_search as web_search_tool,
    spawn_agent as spawn_tool,
    read_task as read_task_tool,
    set_tasks as set_tasks_tool,
    update_tasks as update_tasks_tool,
    update_goal as update_goal_tool,
    open_preview as open_preview_tool,
    build_web_preview_result,
    list_mcp_tools as list_mcp_tools_tool,
    call_mcp_tool as call_mcp_tool_tool,
    call_mcp_tool_with_events,
    list_mcp_resources as list_mcp_resources_tool,
    read_mcp_resource as read_mcp_resource_tool,
    read_file as read_file_tool,
    find_files as find_files_tool,
    search_content as search_content_tool,
    edit_file as edit_file_tool,
    write_file as write_file_tool,
    fetch_url as fetch_url_tool,
    ask_user as ask_user_tool,
    load_skill as load_skill_tool,
)
from harness.core.background import (
    BackgroundJobs,
    BackgroundCompletion,
    background_completion_event,
    background_include_result,
    bind_background_jobs,
    unbind_background_jobs,
)

from harness.tools import file_tools

from harness.core.handoff import (
    build_task,
    serialize_task,
)
from harness.core.memories import load_memories, memories_payload
from harness.core.skills import load_skills, enabled_skills, skills_for_agent, skills_payload
from harness.core.instructions import load_instructions
from harness.identifiers import new_id


class BashAllowRule(BaseModel):
    """Structured output for an 'always allow' rule: the command pattern(s) to
    auto-allow for the rest of the session (e.g. ``["cat *", "ls *"]``)."""
    patterns: list[str]


class BashPermissionDecision(BaseModel):
    """Structured decision for automatic bash permission classification."""

    action: Literal["auto_approve", "escalate"]
    justification: str
    risk: Literal["low", "medium", "high"]


def build_chat_model(
    model_identifier: str,
    global_configuration: "GlobalConfiguration",
    agent_configuration: "AgentConfiguration",
) -> ChatLiteLLMModel:
    """Build the LiteLLM-backed chat model for a provider-qualified model id.

    Every provider flows through one ``ChatLiteLLMModel`` (LiteLLM owns each
    provider's auth, base URL, request format, and reasoning normalization). A
    bare model id with no provider prefix defaults to the OpenCode gateway and is
    qualified as ``opencode/<model>``."""
    if "/" not in model_identifier:
        model_identifier = f"opencode/{model_identifier}"
    resolved = resolve_litellm(
        model_identifier,
        global_configuration.configured_provider_keys(),
        global_configuration.configured_provider_bases(),
    )
    return ChatLiteLLMModel(
        model=resolved["model"],
        api_key=SecretStr(resolved["api_key"]) if resolved["api_key"] else None,
        api_base=resolved["api_base"] or None,
        temperature=0,
        reasoning_effort=agent_configuration.reasoning_effort,
    )


class StreamEvent:
    class Type(str, Enum):
        SESSION = "session"
        STATUS = "status"
        THINKING = "thinking"
        THINKING_DONE = "thinking_done"
        TEXT_CHUNK = "text_chunk"
        TOOL_CALL = "tool_call"
        TOOL_RESULT = "tool_result"
        MCP_EVENT = "mcp_event"
        USAGE = "usage"
        DONE = "done"
        BACKGROUND_STARTED = "background_started"
        PERMISSION_REQUEST = "permission_request"
        QUESTION = "question"
        TASKS_UPDATED = "tasks_updated"
        ERROR = "error"
        DENIED_INJECTION = "denied_injection"
        AGENT_GROUP_STARTED = "agent_group_started"
        AGENT_TEXT_CHUNK = "agent_text_chunk"
        AGENT_TOOL_CALL = "agent_tool_call"
        AGENT_TOOL_RESULT = "agent_tool_result"
        AGENT_MCP_EVENT = "agent_mcp_event"
        AGENT_THINKING = "agent_thinking"
        AGENT_THINKING_DONE = "agent_thinking_done"
        AGENT_STATUS = "agent_status"
        AGENT_DONE = "agent_done"
        STEERING = "steering"

    def __init__(self, event_type: Type, **data):
        self.type = event_type
        self.data = data

    def to_dict(self) -> dict:
        return {"type": self.type.value, "timestamp": datetime.now(timezone.utc).isoformat(), **self.data}

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


def _maybe_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


# Background-task handles minted by the tool registries: web_search ids carry the
# "search-" prefix, background bash the "bg-" prefix. These are NOT A2A tasks and
# can never be read with read_task — their results are auto-delivered when ready.
_BACKGROUND_HANDLE_PREFIXES = {
    "search-": "web_search",
    "bg-": "bash",
}
MAXIMUM_MODEL_RESULT_CHARS = 1 << 16


def _coerce_mcp_arguments(value: Any) -> dict:
    """Normalize the `arguments` of a call_mcp_tool call to a dict. Models often
    emit the nested arguments object as a JSON *string* rather than a real object;
    the previous `isinstance(dict)`-only guard silently dropped those to `{}`, so
    the MCP server saw every field as undefined. Parse a JSON string back to the
    dict it represents; fall back to empty only when there is genuinely nothing
    usable."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _background_handle_kind(task_id: str) -> str | None:
    """The background-task kind ("web_search"/"bash") if ``task_id`` is one of
    those handles rather than a readable A2A task; otherwise ``None``."""
    for prefix, kind in _BACKGROUND_HANDLE_PREFIXES.items():
        if task_id.startswith(prefix):
            return kind
    return None


def _cap_model_result_payload(result: str, *, code: str = "tool_result_truncated") -> str:
    """Keep model-facing tool results bounded while preserving a full-output file."""
    if len(result) <= MAXIMUM_MODEL_RESULT_CHARS:
        return result
    output_path = Path("/tmp") / f"{new_id('tool-result')}.json"
    output_path.write_text(result)
    preview = result[:MAXIMUM_MODEL_RESULT_CHARS]
    parsed = _maybe_json(result)
    if isinstance(parsed, dict):
        payload = {
            **parsed,
            "truncated": True,
            "full_output_file": str(output_path),
            "output_preview": preview,
        }
        for large_key in ("output", "content", "summary", "results"):
            if large_key in payload:
                payload.pop(large_key, None)
        return json.dumps(payload, ensure_ascii=False)
    return json.dumps({
        "code": code,
        "truncated": True,
        "full_output_file": str(output_path),
        "output_preview": preview,
        "size": len(result),
    }, ensure_ascii=False)


def _utc_timestamp(datetime_value: datetime) -> str:
    return datetime_value.isoformat()


def _tool_timing_metadata(
    *,
    tool_name: str,
    tool_call_identifier: str,
    started_at: datetime,
    completed_at: datetime,
    duration_milliseconds: int,
    background_task_identifier: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "tool_name": tool_name,
        "tool_call_id": tool_call_identifier,
        "started_at": _utc_timestamp(started_at),
        "completed_at": _utc_timestamp(completed_at),
        "duration_ms": duration_milliseconds,
    }
    if background_task_identifier:
        metadata["background_task_identifier"] = background_task_identifier
    return metadata


def _model_visible_tool_result(content: str, metadata: dict[str, Any]) -> str:
    return json.dumps({
        "tool_metadata": metadata,
        "result": _maybe_json(content),
    }, ensure_ascii=False)


def _content_text(content: str | list) -> str:
    """The plain-text form of a message content that may be a multimodal list.

    A multimodal user turn carries a content list (text blocks interleaved with
    ``image_url`` blocks); the event-log recorder and any string-only consumer
    want just the prose, so the text blocks are concatenated and the media blocks
    dropped."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(parts)


def _detect_workspace(working_directory: str) -> tuple[str, bool]:
    """Return ``(workspace_root, is_git_repo)``. Walks up from the working
    directory for a ``.git`` marker; if found the workspace root is the repo
    top level, otherwise it falls back to the working directory itself."""
    base = Path(working_directory).expanduser().resolve() if working_directory else Path.cwd().resolve()
    current = base
    while True:
        if (current / ".git").exists():
            return str(current), True
        if current == current.parent:
            break
        current = current.parent
    return str(base), False


def _build_tools(
    agent_configuration: AgentConfiguration,
    global_configuration: GlobalConfiguration,
    *,
    is_sub_agent: bool = False,
) -> list[BaseTool]:
    available = [
        bash_tool,
        read_file_tool,
        find_files_tool,
        search_content_tool,
        edit_file_tool,
        write_file_tool,
        fetch_url_tool,
        load_skill_tool,
        web_search_tool,
        set_tasks_tool,
        update_tasks_tool,
        update_goal_tool,
        read_task_tool,
    ]
    if not is_sub_agent:
        available.append(open_preview_tool)
        available.append(ask_user_tool)
    if agent_configuration.tools.spawn_agent.enabled:
        available.append(spawn_tool)
    if global_configuration.mcp.enabled_servers():
        available.extend([
            list_mcp_tools_tool,
            call_mcp_tool_tool,
            list_mcp_resources_tool,
            read_mcp_resource_tool,
        ])
    return available


class SubAgentRunner:
    def __init__(
        self,
        agent_configuration: AgentConfiguration,
        global_configuration: GlobalConfiguration,
        task_identifier: str,
        prompt: str,
        stream_progress: bool = True,
        read_only_override: Optional[bool] = None,
        working_directory: str = "",
        project_directory: str = "",
    ):
        self.task_identifier = task_identifier
        self.prompt = prompt
        self._stream_progress = stream_progress
        self._agent_name = agent_configuration.identifier
        self._runtime = AgentRuntime(
            agent_configuration=agent_configuration,
            global_configuration=global_configuration,
            working_directory=working_directory,
            project_directory=project_directory,
            is_sub_agent=True,
        )
        # An explicit override (from the spawning call or step)
        # wins over the agent profile's own permission_mode.
        if read_only_override is not None:
            self._runtime.set_read_only(read_only_override)

    async def run_stream(self, always_yield_text: bool = False) -> AsyncIterator[StreamEvent]:
        """Yield each event as the sub-agent produces it, guaranteeing the run
        ends with a non-empty final report.

        The final DONE event always carries the artifact text in ``text``.
        """
        outcome = {"text": "", "stop_reason": "completed"}
        async for event in self._drain(self.prompt, always_yield_text, outcome):
            yield event

        # If the agent produced no deliverable (and was not cancelled), force one
        # by re-prompting for a self-contained conclusion. The conversation is
        # preserved, so the agent still has all the context it gathered.
        if not outcome["text"].strip() and outcome["stop_reason"] != "cancelled":
            conclusion_prompt = self._runtime._prompt_loader.load("conclusion", {})
            async for event in self._drain(conclusion_prompt, always_yield_text, outcome):
                yield event

        done_event = StreamEvent(
            StreamEvent.Type.DONE, text=outcome["text"], stop_reason=outcome["stop_reason"],
        )
        yield done_event

    async def _drain(
        self, prompt: str, always_yield_text: bool, outcome: dict,
    ) -> AsyncIterator[StreamEvent]:
        """Stream one turn through the inner runtime, forwarding events and
        recording ``(text, stop_reason)`` into ``outcome``. DONE events are
        swallowed so :meth:`run_stream` can emit a single terminal DONE."""
        async for event in self._runtime.stream(prompt):
            if event.type == StreamEvent.Type.DONE:
                text = event.data.get("text", "")
                if text.strip():
                    outcome["text"] = text
                outcome["stop_reason"] = event.data.get("stop_reason", outcome["stop_reason"])
                continue
            if event.type == StreamEvent.Type.TEXT_CHUNK:
                if self._stream_progress or always_yield_text:
                    yield event
                continue
            if event.type == StreamEvent.Type.ERROR:
                outcome["stop_reason"] = "error"
                if not outcome["text"]:
                    outcome["text"] = event.data.get("message", "unknown")
            yield event

    async def run_to_task(self) -> Task:
        """Run the agent to completion and return its outcome as an A2A Task."""
        final_text = ""
        stop_reason = "completed"
        async for event in self.run_stream():
            if event.type == StreamEvent.Type.DONE:
                final_text = event.data.get("text", final_text)
                stop_reason = event.data.get("stop_reason", stop_reason)
        state = TaskState.completed
        if stop_reason == "error":
            state = TaskState.failed
        elif stop_reason == "cancelled":
            state = TaskState.canceled
        elif stop_reason == "maximum_iterations":
            state = TaskState.failed
            if not final_text.strip():
                final_text = "Reached the tool-call cap without producing a final answer."
        if not final_text.strip():
            final_text = "Agent produced no output."
            state = TaskState.failed
        return build_task(self.task_identifier, self._agent_name, state, final_text)

    async def run(self) -> str:
        """Return the agent's outcome as a serialized A2A Task (used by
        spawn_agent). The structured task — status + artifacts — is handed to the
        parent verbatim rather than flattened to a bare string."""
        task = await self.run_to_task()
        return json.dumps(serialize_task(task))


class TodoTask(BaseModel):
    identifier: str = ""
    description: str
    status: str = "pending"
    dependencies: list[str] = []


class TaskManager:
    def __init__(self):
        self._tasks: list[TodoTask] = []
        self._next_identifier: int = 1

    def add_tasks(self, task_definitions: list[dict]) -> list[str]:
        created = []
        for definition in task_definitions:
            identifier = f"task-{self._next_identifier}"
            self._next_identifier += 1
            task = TodoTask(
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
            for task in self._tasks:
                if task.identifier == task_id:
                    task.status = status
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


class AgentRuntime:
    _GOAL_CONTINUATION_LIMIT = 3
    # Sub-agents (delegation depth > 0) get a tighter iteration budget than the
    # top-level chat agent so a looping sub-agent fails fast instead of burning
    # the full budget on redundant calls.
    _SUB_AGENT_MAXIMUM_ITERATIONS = 512

    def __init__(
        self,
        agent_configuration: AgentConfiguration,
        global_configuration: GlobalConfiguration,
        pending_permissions: Optional[dict[str, asyncio.Future]] = None,
        pending_questions: Optional[dict[str, asyncio.Future]] = None,
        on_record_event: Optional[callable] = None,
        on_record_message: Optional[callable] = None,
        session_id: str = "",
        conversation: Optional[list] = None,
        working_directory: str = "",
        project_directory: str = "",
        is_sub_agent: bool = False,
        file_lease_manager: FileLeaseManager | None = None,
    ):
        self._session_id = session_id
        self._agent_configuration = agent_configuration
        self._global_configuration = global_configuration
        self._pending_permissions = pending_permissions if pending_permissions is not None else {}
        self._pending_questions = pending_questions if pending_questions is not None else {}
        self._on_record_event = on_record_event
        self._on_record_message = on_record_message
        self._working_directory = working_directory or str(Path.home())
        self._project_directory = project_directory or self._working_directory

        # Precedence: a per-agent override (its model + provider combined), then
        # the configured selected model (likewise combined into provider/model).
        effective_model = (
            agent_configuration.model_identifier
            or global_configuration.selected_model_identifier()
        )

        self._llm = build_chat_model(
            effective_model, global_configuration, agent_configuration
        )

        self._is_sub_agent = is_sub_agent
        self._file_lease_manager = file_lease_manager
        self._tools = _build_tools(
            agent_configuration,
            global_configuration,
            is_sub_agent=is_sub_agent,
        )
        self._bound_llm = self._llm.bind_tools(
            self._tools,
            parallel_tool_calls=True,
        )
        self._permissions = PermissionEvaluator(agent_configuration)
        self._background = BackgroundJobs(
            context_id=session_id,
            agent_name=agent_configuration.identifier,
        )
        # Command patterns the user chose to "always allow" this session — matching
        # bash commands then skip the sandbox/approval prompts. Scoped to this
        # runtime (this context), populated on demand from an LLM-derived rule.
        self._session_allow_patterns: list[str] = []

        self._conversation: list = conversation if conversation is not None else []
        self._system_prompt = agent_configuration.system_prompt
        # Files the model has read this session, keyed by canonical path with the
        # content hash from the last read. Mutating tools compare against this so
        # stale line numbers cannot overwrite externally changed content.
        self._read_files: dict[str, str] = {}
        # How many delegation hops led to this runtime (0 = top-level chat agent).
        self._delegation_depth: int = 0
        self._calls_this_turn: int = 0
        self._abort_event = asyncio.Event()
        # Running token totals for the session, summed from the real usage each
        # model call reports (LiteLLM ``usage`` -> message ``usage_metadata``).
        self._token_usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cache_read_tokens": 0,
            "reasoning_tokens": 0,
            "model_calls": 0,
        }

        prompts_directory = Path(__file__).parent / "prompts"
        self._prompt_loader = PromptLoader(prompts_directory)
        self._cached_system_prompt: str | None = None
        self._task_manager = TaskManager()
        self._active_goal: str = ""
        self._execution_history: list[dict] = []
        self._bypass_permissions: bool = agent_configuration.permission_mode == "bypass"
        self._read_only: bool = agent_configuration.permission_mode == "read_only"
        self._auto_permissions: bool = agent_configuration.permission_mode == "auto"
        # When set, sub-agents (spawn_agent calls) are invoked
        # through this delegate — an A2A call to the target agent's served
        # endpoint — instead of being run in-process. Bound to the A2A context.
        self._delegate: Optional[Callable] = None
        self._a2a_task_id: str = ""
        # Reads another A2A task (sibling/sub-agent) by id from the shared store,
        # so context-aware agents can coordinate. Injected by the executor.
        self._task_reader: Optional[Callable] = None
        self._steering_messages: asyncio.Queue[str] = asyncio.Queue()
        self._steering_available = asyncio.Event()
        self._active_tool_tasks: dict[str, asyncio.Task] = {}

    def _canonical_working_directory(self) -> str:
        return str(Path(self._working_directory or Path.home()).expanduser().resolve(strict=False))

    def _canonical_file_path(self, file_path: str) -> str:
        return str(file_tools.resolve_path(self._working_directory, file_path).expanduser().resolve(strict=False))

    async def _acquire_filesystem_lease(self, *, scope: str, path: str, description: str) -> str:
        if self._file_lease_manager is None:
            return ""
        return await self._file_lease_manager.acquire(
            owner_session_id=self._session_id,
            scope=scope,
            path=path,
            working_directory=self._canonical_working_directory(),
            description=description,
        )

    def _release_filesystem_lease(self, token: str) -> None:
        if token and self._file_lease_manager is not None:
            self._file_lease_manager.release(token)

    @property
    def conversation(self) -> list:
        return self._conversation

    @property
    def background_jobs(self) -> "BackgroundJobs":
        """This runtime's background-job runner. The executor's resume pump reads it
        to know when in-flight work has completed."""
        return self._background

    def inject_stored_background_result(
        self, *, kind: str, identifier: str, tool_call_identifier: str, result: str
    ) -> None:
        """Append a background result restored from the durable store as a
        `background_result` message, so a runtime rebuilt after a restart replays it
        to the model exactly like a live completion would."""
        capped_result = _cap_model_result_payload(result, code=f"{kind}_result_truncated")
        metadata = _tool_timing_metadata(
            tool_name=kind,
            tool_call_identifier=tool_call_identifier,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            duration_milliseconds=0,
            background_task_identifier=identifier,
        )
        self._conversation.append(SystemMessage(
            content=json.dumps({
                "type": "background_result",
                "tool_metadata": metadata,
                "result": _maybe_json(capped_result),
            }, ensure_ascii=False),
        ))

    @property
    def token_usage(self) -> dict[str, int]:
        return dict(self._token_usage)

    def _accumulate_usage(self, response: AIMessage) -> "StreamEvent | None":
        """Fold one model call's real token usage into the running session total
        and return a USAGE event carrying both the per-call and cumulative counts.
        Returns ``None`` when the provider reported no usage for this call."""
        usage = getattr(response, "usage_metadata", None)
        if not usage:
            return None
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        total_tokens = int(usage.get("total_tokens", 0) or 0) or (input_tokens + output_tokens)
        cache_read = int((usage.get("input_token_details") or {}).get("cache_read", 0) or 0)
        reasoning = int((usage.get("output_token_details") or {}).get("reasoning", 0) or 0)
        if not (input_tokens or output_tokens or total_tokens):
            return None
        self._token_usage["input_tokens"] += input_tokens
        self._token_usage["output_tokens"] += output_tokens
        self._token_usage["total_tokens"] += total_tokens
        self._token_usage["cache_read_tokens"] += cache_read
        self._token_usage["reasoning_tokens"] += reasoning
        self._token_usage["model_calls"] += 1
        # input_tokens for this (latest) call is the whole prompt — system, history,
        # and the new turn — so it reflects how full the context currently is. Paired
        # with the model's context window, it drives the context-fill indicator.
        model = getattr(self, "_llm", None)
        context_window = model.context_window() if model is not None else 0
        return StreamEvent(
            StreamEvent.Type.USAGE,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cache_read_tokens=cache_read,
            reasoning_tokens=reasoning,
            context_window=context_window,
            cumulative=dict(self._token_usage),
        )

    @property
    def agent_name(self) -> str:
        return self._agent_configuration.identifier

    @property
    def working_directory(self) -> str:
        return self._working_directory

    @property
    def project_directory(self) -> str:
        return self._project_directory

    @property
    def is_read_only(self) -> bool:
        return self._read_only

    def abort(self) -> None:
        self._abort_event.set()
        self._background.cancel_all()
        for task in list(self._active_tool_tasks.values()):
            task.cancel()

    def abort_tool(self, tool_call_identifier: str) -> bool:
        task = self._active_tool_tasks.get(tool_call_identifier)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def enqueue_steering(self, message: str) -> bool:
        text = message.strip()
        if not text:
            return False
        self._steering_messages.put_nowait(text)
        self._steering_available.set()
        return True

    def discard_pending_steering(self) -> None:
        """Drop any steering that was accepted but never drained into the turn — it
        arrived too late to be honored (after the loop's last drain, or while the turn
        was ending/failing). The client re-delivers such messages as a fresh turn on
        stream close, so they must not linger here and get double-applied when the
        next turn drains the runtime at its first model-call boundary."""
        while not self._steering_messages.empty():
            self._steering_messages.get_nowait()
        self._steering_available.clear()

    def _has_queued_steering(self) -> bool:
        return not self._steering_messages.empty()

    def set_read_only(self, read_only: bool) -> None:
        self._read_only = read_only
        if read_only:
            self._bypass_permissions = False
            self._auto_permissions = False

    def set_permission_mode(self, mode: str) -> None:
        if mode not in ("default", "read_only", "bypass", "auto"):
            return
        if mode == "default":
            self._bypass_permissions = self._agent_configuration.permission_mode == "bypass"
            self._read_only = self._agent_configuration.permission_mode == "read_only"
            self._auto_permissions = self._agent_configuration.permission_mode == "auto"
            return
        self._bypass_permissions = mode == "bypass"
        self._read_only = mode == "read_only"
        self._auto_permissions = mode == "auto"

    def set_delegate(self, delegate: Callable) -> None:
        """Install the A2A delegate used to invoke sub-agents as related tasks."""
        self._delegate = delegate

    def set_a2a_task_id(self, task_id: str) -> None:
        """Record the A2A task id of the current turn so delegated sub-agent
        tasks can reference it as their parent."""
        self._a2a_task_id = task_id

    def set_task_reader(self, task_reader: Callable) -> None:
        """Install the reader used by the read_task tool to fetch sibling/sub-agent
        A2A tasks from the shared store."""
        self._task_reader = task_reader

    def set_delegation_depth(self, depth: int) -> None:
        """Record how many delegation hops led to this runtime, so it can refuse
        to delegate past the configured maximum depth."""
        self._delegation_depth = depth

    def _evaluate_bash_permission(self, command: str) -> str:
        if self._bypass_permissions:
            return "allow"
        return self._permissions.evaluate_bash_permission(command)

    async def _classify_bash_permission(
        self,
        *,
        command: str,
        raw_command: str,
        default_decision: str,
        read_only: bool,
        risk: str,
        justification: str,
        static_classification: str = "",
        static_detail: str = "",
        outside_reads: Optional[list[str]] = None,
    ) -> BashPermissionDecision:
        context = json.dumps(
            {
                "working_directory": self._working_directory,
                "command": command,
                "raw_command": raw_command,
                "default_permission_decision": default_decision,
                "model_declared_read_only": read_only,
                "model_declared_risk": risk,
                "model_justification": justification,
                "static_read_only_classification": static_classification,
                "static_detail": static_detail,
                "outside_working_directory_reads": outside_reads or [],
                "allowed_actions": ["auto_approve", "escalate"],
            },
            ensure_ascii=False,
        )
        prompt = self._prompt_loader.load("bash_permission_classifier", {"context": context})
        try:
            model = self._llm.bind_tools([BashPermissionDecision], tool_choice="auto")
            response = await model.ainvoke([
                SystemMessage(content=prompt),
            ])
            if not response.tool_calls:
                return BashPermissionDecision(action="escalate", justification="Classifier returned no structured decision.", risk="medium")
            decision = BashPermissionDecision.model_validate(response.tool_calls[0]["args"])
            if default_decision == "deny" and decision.action == "auto_approve":
                return BashPermissionDecision(action="escalate", justification="User-configured permissions deny this command.", risk="high")
            if not decision.justification.strip():
                return BashPermissionDecision(action="escalate", justification="Classifier did not provide a justification.", risk="medium")
            return decision
        except Exception as exception:
            return BashPermissionDecision(action="escalate", justification=f"{exception}", risk="medium")

    def _command_session_allowed(self, command: str) -> bool:
        """Whether a prior 'always allow' in this session covers this command, so
        it skips the sandbox/approval prompts."""
        if not self._session_allow_patterns:
            return False
        return self._agent_configuration.tools.bash.command_matches(command, self._session_allow_patterns)

    async def _resolve_permission_future(
        self, request_identifier: str, future: "asyncio.Future", command: str, *, is_bash: bool
    ) -> bool:
        """Await the user's decision on a permission request and clean it up.
        Returns whether the command may run; on 'allow_always' for a bash command,
        also schedules a session rule so matching commands won't prompt again."""
        try:
            decision = await future
        finally:
            self._pending_permissions.pop(request_identifier, None)
        if is_bash and decision == "allow_always" and command:
            self._schedule_bash_allow_rule(command)
        return decision != "deny"

    def _schedule_bash_allow_rule(self, command: str) -> None:
        try:
            asyncio.create_task(self._remember_bash_allow_rule(command))
        except RuntimeError:
            pass

    async def _remember_bash_allow_rule(self, command: str) -> None:
        """Ask the model to distill an allow rule (one or more command patterns)
        from the approved command, and add it to this session's allowlist. Best
        effort — the one-time approval already ran the command regardless."""
        try:
            prompt = self._prompt_loader.load("bash_allow_rule", {"command": command})
            # bind_tools + manual parse instead of with_structured_output: the
            # configured reasoning model rejects response_format/forced tool_choice
            # that structured output relies on, but accepts a regular tool call.
            model = self._llm.bind_tools([BashAllowRule], tool_choice="auto")
            response = await model.ainvoke(prompt)
            if not response.tool_calls:
                return
            rule = BashAllowRule.model_validate(response.tool_calls[0]["args"])
            for pattern in (rule.patterns or []):
                pattern = pattern.strip()
                if pattern and pattern not in self._session_allow_patterns:
                    self._session_allow_patterns.append(pattern)
        except Exception:
            pass

    def _record_event(self, event_type: str, data: dict) -> None:
        record = {"type": event_type, "timestamp": _utc_timestamp(datetime.now(timezone.utc)), **data}
        self._execution_history.append(record)
        if self._on_record_event:
            self._on_record_event(event_type, data)

    def _record_message(self, role: str, content: str, tool_call_id: str = "") -> None:
        if self._on_record_message:
            self._on_record_message(role, content, tool_call_id)

    def _build_static_system_prompt(self) -> str:
        """Build the static portion of the system prompt (cached across calls).

        Every agent — main or spawned — is built through this same path, so they
        all share the baseline system prompt, the working-directory/agents
        context, and the available-skills awareness.
        """
        if self._cached_system_prompt is None:
            available_agents = describe_available_agents(
                self._global_configuration.agent_directories_for(self._project_directory)
            )
            all_skills = enabled_skills(load_skills(self._global_configuration.skill_directories_for(self._project_directory)))
            agent_skills = skills_for_agent(all_skills, self._agent_configuration.skills)
            memories = load_memories(self._global_configuration.memory_directories_for(self._project_directory))
            workspace_root, is_git_repo = _detect_workspace(self._working_directory)
            context_json = json.dumps({
                "working_directory": self._working_directory,
                "project_directory": self._project_directory,
                "workspace_root": workspace_root,
                "is_git_repo": is_git_repo,
                "session_workspace_strategy": self._global_configuration.workspace.strategy,
                "platform": platform.system(),
                "today_date": datetime.now().strftime("%Y-%m-%d"),
                "available_agents": available_agents,
                "is_sub_agent": self._is_sub_agent,
            })
            sub_agent_context = "This agent is initialized as the main orchestrator agent."
            if self._is_sub_agent:
                sub_agent_context = self._prompt_loader.load("sub_agent_context", {})
            self._cached_system_prompt = self._prompt_loader.load("system_prompt", {
                "system_prompt": self._system_prompt,
                "context": context_json,
                "instructions": load_instructions(self._project_directory),
                "skills": json.dumps(skills_payload(agent_skills)),
                "memories": json.dumps(memories_payload(memories)),
                "sub_agent_context": sub_agent_context,
            })
        return self._cached_system_prompt

    def _build_dynamic_context(self) -> str:
        """Build the dynamic context injected at the end of the message list."""
        current_datetime = datetime.now(timezone.utc)
        current_timestamp = current_datetime.strftime("%Y-%m-%d %H:%M:%S UTC")
        turn_metadata = {
            "current_timestamp": current_timestamp,
            "current_timestamp_iso": current_datetime.isoformat(),
            "timezone": "UTC",
            "working_directory": self._working_directory or str(Path.cwd()),
            "project_directory": self._project_directory,
            "session_id": self._session_id,
            "agent_name": self._agent_configuration.name,
            "is_sub_agent": self._is_sub_agent,
        }
        turn_reminders = self._prompt_loader.load("turn_reminders", {
            "turn_metadata": json.dumps(turn_metadata, sort_keys=True),
        }).strip()
        parts = []
        if turn_reminders:
            parts.append(turn_reminders)
        parts.append(json.dumps({"pwd": self._working_directory or str(Path.cwd())}))
        if self._active_goal:
            parts.append(json.dumps({"active_goal": self._active_goal}))
        tasks_data = self._task_manager.to_dict_list()
        if tasks_data:
            parts.append(json.dumps({"tasks": tasks_data}))
        parts.append(json.dumps({"background_processes": {
            "running": self._background.active_by_context_key(),
            "active_count": self._background.active_count(),
            "recent_events": self._execution_history[-20:],
        }}))
        return "\n".join(parts)

    def _background_result_events(self) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        for completion in self._background.drain_completed():
            capped_result = _cap_model_result_payload(
                completion.result,
                code=f"{completion.kind}_result_truncated",
            )
            duration_milliseconds = int((completion.completed_at - completion.started_at).total_seconds() * 1000)
            background_metadata = _tool_timing_metadata(
                tool_name=completion.kind,
                tool_call_identifier=completion.tool_call_identifier,
                started_at=completion.started_at,
                completed_at=completion.completed_at,
                duration_milliseconds=duration_milliseconds,
                background_task_identifier=completion.identifier,
            )
            # Append-only: the scheduled placeholder ToolMessage stays put and the
            # result lands as a *new* message. Rewriting the placeholder in place
            # would change the conversation mid-stream and invalidate the provider's
            # prompt cache from that point on — re-billing the whole suffix. The
            # placeholder already satisfies its tool_call, so appending keeps the
            # prefix monotonic (always cacheable) while the model still sees the result.
            self._conversation.append(SystemMessage(
                content=json.dumps({
                    "type": "background_result",
                    "tool_metadata": background_metadata,
                    "result": _maybe_json(capped_result),
                }, ensure_ascii=False),
            ))
            events.append(StreamEvent(
                StreamEvent.Type.TOOL_RESULT,
                id=completion.tool_call_identifier,
                name=completion.kind,
                result=_maybe_json(capped_result),
                task_id=completion.identifier,
            ))
            completion_event_data: dict[str, Any] = {"task_identifier": completion.identifier}
            if background_include_result(completion.kind):
                completion_event_data["result"] = capped_result
            self._record_event(background_completion_event(completion.kind), completion_event_data)
        return events

    def _record_turn(self, user_message: str, tool_calls: list, tool_results: list, final_response: str):
        self._record_message("human", user_message)
        for tool_call_entry in tool_calls:
            self._record_message("tool", json.dumps(tool_call_entry.get("args", {})), tool_call_entry.get("name", ""))
        for tool_result_entry in tool_results:
            self._record_message("tool", str(tool_result_entry.get("result", "")), tool_result_entry.get("name", ""))
        if final_response:
            self._record_event("assistant_response_completed", {
                "content_characters": len(final_response),
                "tool_call_count": len(tool_calls),
                "tool_result_count": len(tool_results),
            })
        self._record_message("ai", final_response)

    async def _drain_steering_messages(self) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        while not self._steering_messages.empty():
            message = self._steering_messages.get_nowait()
            self._conversation.append(HumanMessage(content=message))
            events.append(StreamEvent(StreamEvent.Type.STEERING, text=message))
        if self._steering_messages.empty():
            self._steering_available.clear()
        return events

    def _invalid_tool_call_content(self, invalid: dict) -> str:
        """Build the message for a malformed tool call — used both as the tool
        result the model sees and the error surfaced to the user. The wording
        lives in the loaded ``invalid_tool_call`` prompt template so it stays
        out of code; a missing template degrades to an empty string, which still
        pairs the tool_call_id with a (blank) tool message and keeps the
        conversation valid."""
        return self._prompt_loader.load("invalid_tool_call", {
            "name": invalid.get("name") or "unknown",
            "error": invalid.get("error") or "arguments could not be parsed",
        })

    def preview_render_error_note(self, payload: str) -> str:
        """Frame a preview render failure as a behind-the-scenes self-realization
        note (injected as a system message, not user input) the model repairs. The
        raw error rides along as its JSON payload, intact."""
        return self._prompt_loader.load("preview_render_error", {"payload": payload})

    async def stream(
        self, user_message: str | list, as_system_note: bool = False
    ) -> AsyncIterator[StreamEvent]:
        self._abort_event.clear()
        self._calls_this_turn = 0

        # A turn's input is usually plain text, but an attachment turn carries a
        # multimodal content list (a text block plus one image_url block per
        # attached image) so a vision model actually sees the pixels. LangChain's
        # HumanMessage accepts either, and the model adapter passes the content
        # straight through to the provider. The event-log recorder only wants a
        # string, so a flattened text form is derived for it.
        recorded_user_message = user_message if isinstance(user_message, str) else _content_text(user_message)

        # A self-realization note (e.g. a widget render error) enters the
        # conversation as a system message so the model treats it as its own
        # observation, not as something the user said.
        self._conversation.append(
            SystemMessage(content=user_message) if as_system_note else HumanMessage(content=user_message)
        )

        turn_tool_calls_log: list[dict] = []
        turn_tool_results_log: list[dict] = []
        turn_final_response = ""
        goal_continuations = 0

        effective_maximum_iterations = self._agent_configuration.maximum_iterations
        if self._delegation_depth > 0:
            effective_maximum_iterations = min(
                effective_maximum_iterations, self._SUB_AGENT_MAXIMUM_ITERATIONS
            )

        while self._calls_this_turn < effective_maximum_iterations:
            if self._abort_event.is_set():
                if self._has_queued_steering():
                    self._abort_event.clear()
                    for steering_event in await self._drain_steering_messages():
                        yield steering_event
                    self._calls_this_turn += 1
                    continue
                yield StreamEvent(StreamEvent.Type.DONE, text="", stop_reason="cancelled")
                return

            background_events = self._background_result_events()
            if background_events:
                for background_event in background_events:
                    yield background_event
                self._calls_this_turn += 1
                continue

            # In-flight background work no longer holds the turn open. Completed
            # results are drained above and delivered mid-turn while the model is
            # still working; if the model goes idle with work still pending, the turn
            # simply ends and the executor's resume pump wakes the agent with an
            # autonomous turn the moment the next result lands.
            for steering_event in await self._drain_steering_messages():
                yield steering_event

            # Dynamic context (turn reminders, time, pwd, active goal) is injected
            # only on the first iteration of a turn — when the user just sent a
            # message. Subsequent iterations (after tool calls) skip it to avoid
            # re-sending the same reminders on every LLM call within the turn.
            dynamic_parts = (
                [SystemMessage(content=self._build_dynamic_context())]
                if self._calls_this_turn == 0 else []
            )
            messages = (
                [SystemMessage(content=self._build_static_system_prompt())]
                + self._conversation
                + dynamic_parts
            )

            # Open a thinking step for this iteration. One channel (THINKING)
            # drives the indicator: this bare ping marks "reasoning started" and
            # reasoning_content fills the body — no labels, no separate status
            # placeholder to reconcile downstream. We time the phase here and emit
            # a matching THINKING_DONE the moment reasoning ends (the first answer
            # token, or — for a tool-only turn — when the stream closes), so the UI
            # can show "Thought for Ns". Measured server-side as wall-clock and
            # carried in the event, so it is correct on live stream and on replay.
            yield StreamEvent(StreamEvent.Type.THINKING)
            thinking_started_at = time.monotonic()
            thinking_done_emitted = False
            response_chunks: list[AIMessageChunk] = []
            aborted_for_steering = False
            async for chunk in self._bound_llm.astream(messages):
                if self._abort_event.is_set():
                    if self._has_queued_steering():
                        self._abort_event.clear()
                        aborted_for_steering = True
                        break
                    yield StreamEvent(StreamEvent.Type.DONE, text="", stop_reason="cancelled")
                    return
                response_chunks.append(chunk)
                if chunk.content:
                    # First answer token: reasoning is over. Close the thinking
                    # phase before the text streams so the indicator flips to
                    # "Thought for Ns" exactly as the answer begins.
                    if not thinking_done_emitted:
                        thinking_done_emitted = True
                        yield StreamEvent(
                            StreamEvent.Type.THINKING_DONE,
                            duration_ms=int((time.monotonic() - thinking_started_at) * 1000),
                        )
                    if self._agent_configuration.stream_agent_progress:
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
            # A tool-only turn produces no answer text, so close the phase here.
            if not thinking_done_emitted:
                yield StreamEvent(
                    StreamEvent.Type.THINKING_DONE,
                    duration_ms=int((time.monotonic() - thinking_started_at) * 1000),
                )
            if aborted_for_steering:
                for steering_event in await self._drain_steering_messages():
                    yield steering_event
                self._calls_this_turn += 1
                continue
            response = add_ai_message_chunks(response_chunks[0], *response_chunks[1:]) if response_chunks else AIMessageChunk(content="")

            usage_event = self._accumulate_usage(response)
            if usage_event is not None:
                yield usage_event

            # Malformed tool calls (arguments that failed JSON parsing) land in
            # `invalid_tool_calls` while `tool_calls` may be empty. LangChain
            # still serializes invalid_tool_calls into the API payload as
            # `tool_calls`, so each one MUST be followed by a tool message —
            # otherwise the next provider call fails with "insufficient tool
            # messages following tool_calls". Ensure every invalid call carries
            # an id that matches the ToolMessage appended for it below.
            for invalid in response.invalid_tool_calls:
                if not invalid.get("id"):
                    invalid["id"] = f"call_invalid_{uuid.uuid4().hex[:24]}"

            if not response.tool_calls:
                if response.invalid_tool_calls:
                    # A response carrying only malformed tool calls (arguments that
                    # failed to parse). These are NOT valid tool_calls — the LiteLLM
                    # model serializes only message.tool_calls, never
                    # invalid_tool_calls — so a ToolMessage response would be
                    # orphaned, and strict providers (e.g. DeepSeek) reject that with
                    # "Messages with role 'tool' must follow a tool_calls message".
                    # Correct the model with a system note and let it retry. This is
                    # model-facing, so it is not surfaced to the user.
                    if response.content:
                        self._conversation.append(response)
                    for invalid in response.invalid_tool_calls:
                        self._conversation.append(SystemMessage(
                            content=self._invalid_tool_call_content(invalid),
                        ))
                    self._calls_this_turn += 1
                    continue

                # The model produced no tool calls. Any still-running background work
                # does not hold the turn open: it ends here, and the executor's resume
                # pump wakes the agent with an autonomous turn once the next result
                # lands. Results that already completed were drained at the top of the
                # loop, so nothing in hand is lost by finishing now.
                final_text = response.content or ""
                turn_final_response = final_text
                self._conversation.append(response)
                steering_events = await self._drain_steering_messages()
                if steering_events:
                    for steering_event in steering_events:
                        yield steering_event
                    self._calls_this_turn += 1
                    continue
                if self._active_goal and goal_continuations < self._GOAL_CONTINUATION_LIMIT:
                    goal_continuations += 1
                    self._calls_this_turn += 1
                    goal_continuation = self._prompt_loader.load("goal_continuation", {"goal": self._active_goal})
                    self._conversation.append(SystemMessage(content=goal_continuation))
                    yield StreamEvent(
                        StreamEvent.Type.STATUS,
                        code="goal_check",
                    )
                    continue
                self._calls_this_turn = 0
                self._record_turn(
                    recorded_user_message, turn_tool_calls_log,
                    turn_tool_results_log, turn_final_response,
                )
                yield StreamEvent(StreamEvent.Type.DONE, text=final_text, stop_reason="completed")
                return

            # Collect each tool's outcome as it runs, then append the AIMessage
            # and all ToolMessages afterward. Appending together (rather than as
            # tools finish) keeps the conversation valid even if a tool is
            # aborted mid-flight — every tool_call always gets a ToolMessage.
            outcomes: dict[str, dict] = {}

            if response.tool_calls and not self._abort_event.is_set():
                async for event in self._drain_tools_concurrently(
                    response.tool_calls, turn_tool_calls_log, turn_tool_results_log, outcomes,
                ):
                    yield event

            # Append the initiating AIMessage, then a ToolMessage for every call.
            self._conversation.append(response)
            for tool_call_data in response.tool_calls:
                tool_call_identifier = tool_call_data["id"]
                outcome = outcomes.get(tool_call_identifier, {})
                content = outcome.get("content", "")
                if not content:
                    content = "(interrupted)" if self._abort_event.is_set() else ""
                model_visible_content = _model_visible_tool_result(
                    content,
                    outcome.get("metadata") or _tool_timing_metadata(
                        tool_name=tool_call_data.get("name", ""),
                        tool_call_identifier=tool_call_identifier,
                        started_at=datetime.now(timezone.utc),
                        completed_at=datetime.now(timezone.utc),
                        duration_milliseconds=0,
                    ),
                )
                self._conversation.append(
                    ToolMessage(content=model_visible_content, tool_call_id=tool_call_identifier)
                )
                background_task_identifier = outcome.get("background_task_identifier")
                if background_task_identifier:
                    self._background.bind_tool_call(
                        background_task_identifier, tool_call_identifier,
                    )
                denied_commands = outcome.get("denied_commands", [])
                if denied_commands:
                    commands_list = ", ".join(f"'{command}'" for command in denied_commands)
                    denied_message = self._prompt_loader.load("command_denied", {"commands": commands_list})
                    self._conversation.append(SystemMessage(content=denied_message))

            # Malformed tool calls serialized alongside valid ones: correct them
            # with a system note (not a ToolMessage — invalid calls aren't in the
            # serialized tool_calls, so a ToolMessage would be orphaned and rejected
            # by strict providers). Model-facing; not surfaced to the user.
            for invalid in response.invalid_tool_calls:
                self._conversation.append(SystemMessage(
                    content=self._invalid_tool_call_content(invalid),
                ))

            if self._abort_event.is_set():
                if self._has_queued_steering():
                    self._abort_event.clear()
                    for steering_event in await self._drain_steering_messages():
                        yield steering_event
                    self._calls_this_turn += 1
                    continue
                self._record_turn(recorded_user_message, turn_tool_calls_log, turn_tool_results_log, "")
                yield StreamEvent(StreamEvent.Type.DONE, text="", stop_reason="cancelled")
                return

            for steering_event in await self._drain_steering_messages():
                yield steering_event

            self._calls_this_turn += 1

        self._record_turn(
            recorded_user_message, turn_tool_calls_log,
            turn_tool_results_log, "",
        )
        yield StreamEvent(
            StreamEvent.Type.DONE,
            text="Reached the tool-call limit without producing a final answer.",
            stop_reason="maximum_iterations",
        )

    def _load_sub_agent(self, name: str) -> AgentConfiguration:
        return load_agent_configuration(
            name,
            self._global_configuration.agent_directories_for(self._project_directory),
        )

    def _build_sub_agent_prompt(self, prompt: str, read_only: bool | None) -> str:
        mode = "read-only investigation" if read_only else "delegated task"
        return self._prompt_loader.load("sub_agent_task", {
            "mode": mode,
            "prompt": prompt,
        })

    async def _run_one_tool(
        self,
        tool_call_data: dict,
        turn_tool_calls_log: list[dict],
        turn_tool_results_log: list[dict],
        outcomes: dict[str, dict],
    ) -> AsyncIterator[StreamEvent]:
        """Run a single tool call, yielding its events and recording its outcome
        in ``outcomes`` (keyed by tool_call_id). The caller appends ToolMessages
        afterward so the conversation stays consistent even on abort.

        Self-contained so it can run concurrently with other tools: each owns its
        TOOL_CALL emit, result collection, and outcome record.
        """
        tool_name = tool_call_data["name"]
        tool_arguments = tool_call_data["args"]
        tool_call_identifier = tool_call_data["id"]
        started_at = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()

        yield StreamEvent(
            StreamEvent.Type.TOOL_CALL,
            name=tool_name,
            arguments=tool_arguments,
            id=tool_call_identifier,
        )
        turn_tool_calls_log.append({
            "name": tool_name,
            "arguments": tool_arguments,
            "tool_call_id": tool_call_identifier,
            "started_at": _utc_timestamp(started_at),
        })

        result_content: str = ""
        background_task_identifier: str | None = None
        denied_commands: list[str] = []

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
                        background_task_identifier = (
                            raw_task_identifier if isinstance(raw_task_identifier, str) else None
                        )
                        result_content = json.dumps({
                            "code": "background_task_scheduled",
                            "task_identifier": background_task_identifier,
                        })
                        turn_tool_results_log.append({"name": tool_name, "result": json.dumps(result_str)})
                    else:
                        if isinstance(result_str, dict):
                            model_context = result_str.get("model_context")
                            if model_context is not None:
                                result_str = json.dumps(model_context)
                            else:
                                result_str = json.dumps(result_str)
                        result_content = _cap_model_result_payload(str(result_str))
                        turn_tool_results_log.append({"name": tool_name, "result": result_content})
                elif event.type == StreamEvent.Type.ERROR:
                    result_content = event.data.get("message", "unknown error")
                    turn_tool_results_log.append({"name": tool_name, "result": result_content})
                elif event.type == StreamEvent.Type.DENIED_INJECTION:
                    denied_commands.append(event.data.get("command", ""))
                elif event.type == StreamEvent.Type.TASKS_UPDATED:
                    result_content = event.data.get("result_message", "")
                    turn_tool_results_log.append({"name": tool_name, "result": result_content})
                elif event.type == StreamEvent.Type.BACKGROUND_STARTED:
                    raw_task_identifier = event.data.get("task_id")
                    background_task_identifier = (
                        raw_task_identifier if isinstance(raw_task_identifier, str) else None
                    )
                    result_content = json.dumps({
                        "code": "background_task_scheduled",
                        "task_identifier": background_task_identifier,
                    })
                    turn_tool_results_log.append(
                        {"name": tool_name, "result": event.data.get("result_message", "")}
                    )
        except asyncio.CancelledError:
            result_content = "Tool call aborted."
            yield StreamEvent(
                StreamEvent.Type.ERROR, id=tool_call_identifier, message=result_content, tool=tool_name,
            )
            turn_tool_results_log.append({"name": tool_name, "result": result_content})
        except Exception as exception:
            result_content = f"{exception}"
            yield StreamEvent(
                StreamEvent.Type.ERROR, id=tool_call_identifier, message=result_content, tool=tool_name,
            )
            turn_tool_results_log.append({"name": tool_name, "result": result_content})

        completed_at = datetime.now(timezone.utc)
        duration_milliseconds = int((time.monotonic() - started_monotonic) * 1000)
        timing_metadata = _tool_timing_metadata(
            tool_name=tool_name,
            tool_call_identifier=tool_call_identifier,
            started_at=started_at,
            completed_at=completed_at,
            duration_milliseconds=duration_milliseconds,
            background_task_identifier=background_task_identifier,
        )

        outcomes[tool_call_identifier] = {
            "content": result_content,
            "background_task_identifier": background_task_identifier,
            "denied_commands": denied_commands,
            "metadata": timing_metadata,
        }

    async def _drain_tools_concurrently(
        self,
        tool_calls: list[dict],
        turn_tool_calls_log: list[dict],
        turn_tool_results_log: list[dict],
        outcomes: dict[str, dict],
    ) -> AsyncIterator[StreamEvent]:
        """Run independent tool calls concurrently, yielding their events as they
        arrive (interleaved). With ``parallel_tool_calls=True`` the model emits
        several calls in one response; running them concurrently means multiple
        spawned agents (and other tools) progress in parallel rather than
        sequentially."""
        if not tool_calls:
            return

        queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        remaining = len(tool_calls)

        async def runner(tool_call_data: dict) -> None:
            nonlocal remaining
            tool_call_identifier = tool_call_data["id"]
            current_task = asyncio.current_task()
            if current_task is not None:
                self._active_tool_tasks[tool_call_identifier] = current_task
            try:
                async for event in self._run_one_tool(
                    tool_call_data, turn_tool_calls_log, turn_tool_results_log, outcomes,
                ):
                    await queue.put(event)
            except Exception:
                # _run_one_tool handles its own errors; this guards the merge.
                pass
            finally:
                self._active_tool_tasks.pop(tool_call_identifier, None)
                remaining -= 1
                if remaining == 0:
                    await queue.put(None)

        tasks = [asyncio.create_task(runner(call)) for call in tool_calls]
        try:
            while True:
                if self._abort_event.is_set():
                    break
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _validate_tool_call(self, tool_name: str, arguments: dict) -> tuple[str, str] | None:
        if tool_name in ("bash", "call_mcp_tool"):
            risk = arguments.get("risk", "low")
            if risk not in ("low", "medium", "high"):
                return ("invalid_risk", f"risk must be one of 'low', 'medium', 'high', got '{risk}'.")
            read_only = arguments.get("read_only", True)
            if not isinstance(read_only, bool):
                return ("invalid_read_only", "read_only must be a boolean.")
        if tool_name == "call_mcp_tool":
            if not arguments.get("server"):
                return ("invalid_mcp_server", "server is required.")
            if not arguments.get("tool_name"):
                return ("invalid_mcp_tool", "tool_name is required.")
        return None

    def _path_like_token(self, token: str) -> str:
        if not token or token in ("-", "--"):
            return ""
        if "://" in token:
            return ""
        if token.startswith("--") and "=" in token:
            token = token.split("=", 1)[1]
        elif token.startswith("-"):
            return ""
        token = token.strip("'\"")
        if not token or token in (".",):
            return ""
        if token.startswith(("~", "/", "./", "../")):
            return token
        if "/" in token:
            return token
        return ""

    def _outside_working_directory_reads(self, command: str) -> list[str]:
        """Best-effort static path boundary check for bash commands.

        The shell remains too dynamic to prove every access, so this intentionally
        catches explicit path arguments that leave the session working directory:
        absolute paths, home paths, and parent-directory traversal.
        """
        if not self._global_configuration.sandbox.enabled:
            return []
        root = Path(self._working_directory or Path.home()).expanduser()
        try:
            root = root.resolve(strict=False)
        except OSError:
            return []

        outside: list[str] = []
        seen: set[str] = set()
        for segment in self._agent_configuration.tools.bash._extract_segments(command):
            try:
                tokens = shlex.split(segment)
            except ValueError:
                tokens = segment.split()
            for token in tokens[1:]:
                path_token = self._path_like_token(token)
                if not path_token:
                    continue
                path = Path(path_token).expanduser()
                if not path.is_absolute():
                    path = root / path
                try:
                    resolved = path.resolve(strict=False)
                except OSError:
                    continue
                if resolved == root or resolved.is_relative_to(root):
                    continue
                display = str(Path(path_token).expanduser())
                if display not in seen:
                    seen.add(display)
                    outside.append(display)
        return outside

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

            # A command the user chose to "always allow" this session skips both
            # the sandbox and approval prompts below.
            session_allowed = self._command_session_allowed(raw_command)

            static_classification, static_detail = self._agent_configuration.tools.bash.read_only_assessment(raw_command)
            outside_reads = self._outside_working_directory_reads(raw_command)
            if outside_reads and not session_allowed:
                paths = ", ".join(outside_reads)
                sandbox_message = (
                    f"Sandbox approval required: this command reads outside the working directory ({paths})."
                )
                if self._is_sub_agent:
                    yield StreamEvent(
                        StreamEvent.Type.ERROR,
                        id=tool_call_identifier,
                        code="sandbox_denied",
                        message=sandbox_message,
                        tool=tool_name,
                    )
                    yield StreamEvent(
                        StreamEvent.Type.DENIED_INJECTION,
                        id=tool_call_identifier,
                        command=command,
                    )
                    return
                permission_decision = self._evaluate_bash_permission(command)
                if self._auto_permissions and permission_decision != "deny":
                    decision = await self._classify_bash_permission(
                        command=command,
                        raw_command=raw_command,
                        default_decision=permission_decision,
                        read_only=read_only,
                        risk=risk or "medium",
                        justification=justification or sandbox_message,
                        static_classification=static_classification,
                        static_detail=static_detail,
                        outside_reads=outside_reads,
                    )
                    if decision.action == "auto_approve":
                        self._record_event("bash_auto_approved", {
                            "command": raw_command,
                            "reason": decision.justification,
                            "risk": decision.risk,
                        })
                    else:
                        request_identifier = f"perm-{self._session_id}-{uuid.uuid4()}"
                        future = asyncio.get_event_loop().create_future()
                        self._pending_permissions[request_identifier] = future
                        yield StreamEvent(
                            StreamEvent.Type.PERMISSION_REQUEST,
                            id=tool_call_identifier,
                            request_id=request_identifier,
                            command=command,
                            justification=decision.justification or sandbox_message,
                            risk=decision.risk,
                        )
                        allowed = await self._resolve_permission_future(request_identifier, future, raw_command, is_bash=True)
                        if not allowed:
                            yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message="sandbox read not approved by user", tool=tool_name)
                            return
                else:
                    if permission_decision == "deny":
                        yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message="sandbox read denied by default permissions", tool=tool_name)
                        return
                    request_identifier = f"perm-{self._session_id}-{uuid.uuid4()}"
                    future = asyncio.get_event_loop().create_future()
                    self._pending_permissions[request_identifier] = future
                    yield StreamEvent(
                        StreamEvent.Type.PERMISSION_REQUEST,
                        id=tool_call_identifier,
                        request_id=request_identifier,
                        command=command,
                        justification=sandbox_message,
                        risk="medium",
                    )
                    allowed = await self._resolve_permission_future(request_identifier, future, raw_command, is_bash=True)
                    if not allowed:
                        yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message="sandbox read not approved by user", tool=tool_name)
                        return

            # Read-only agents may only run read-only commands. Static analysis
            # classifies the command; a detected mutation is always blocked, and
            # a command that can't be classified is blocked only when the model
            # itself marked it as a write. This is a hard block — sub-agents have
            # no human in the loop to approve.
            if self._read_only:
                violation = None
                if static_classification == "mutating":
                    violation = static_detail
                elif static_classification == "unknown" and not read_only:
                    violation = "a command not recognized as read-only that you marked as modifying state"
                if violation:
                    deny_message = self._prompt_loader.load("read_only_denied", {"violation": violation})
                    yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message=deny_message, tool=tool_name)
                    yield StreamEvent(
                        StreamEvent.Type.DENIED_INJECTION,
                        id=tool_call_identifier,
                        command=command,
                    )
                    return

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
            elif not read_only and not session_allowed and (permission_decision == "ask" or risk in ("medium", "high")):
                if self._auto_permissions:
                    decision = await self._classify_bash_permission(
                        command=command,
                        raw_command=raw_command,
                        default_decision=permission_decision,
                        read_only=read_only,
                        risk=risk or "medium",
                        justification=justification,
                        static_classification=static_classification,
                        static_detail=static_detail,
                        outside_reads=outside_reads,
                    )
                    if decision.action == "auto_approve":
                        self._record_event("bash_auto_approved", {
                            "command": raw_command,
                            "reason": decision.justification,
                            "risk": decision.risk,
                        })
                    else:
                        request_identifier = f"perm-{self._session_id}-{uuid.uuid4()}"
                        future = asyncio.get_event_loop().create_future()
                        self._pending_permissions[request_identifier] = future
                        yield StreamEvent(
                            StreamEvent.Type.PERMISSION_REQUEST,
                            id=tool_call_identifier,
                            request_id=request_identifier,
                            command=command,
                            justification=decision.justification or justification,
                            risk=decision.risk,
                        )
                        allowed = await self._resolve_permission_future(request_identifier, future, raw_command, is_bash=True)
                        if not allowed:
                            yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message="command not approved by user", tool=tool_name)
                            return
                else:
                    request_identifier = f"perm-{self._session_id}-{uuid.uuid4()}"
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
                    allowed = await self._resolve_permission_future(request_identifier, future, raw_command, is_bash=True)
                    if not allowed:
                        yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message="command not approved by user", tool=tool_name)
                        return

            lease_token = ""
            if not read_only:
                try:
                    lease_token = await self._acquire_filesystem_lease(
                        scope="worktree",
                        path=self._canonical_working_directory(),
                        description=f"mutating bash: {raw_command[:160]}",
                    )
                except FileLeaseConflict as exception:
                    yield StreamEvent(
                        StreamEvent.Type.ERROR,
                        id=tool_call_identifier,
                        code="filesystem_lease_conflict",
                        message=str(exception),
                        tool=tool_name,
                    )
                    return

            try:
                background_token = bind_background_jobs(self._background)
                try:
                    result = await bash_tool.ainvoke(tool_arguments)
                finally:
                    unbind_background_jobs(background_token)
                result_data = _maybe_json(result)
                yield StreamEvent(StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=result_data)
                if isinstance(result_data, dict) and result_data.get("code") == "bash_started":
                    task_identifier = result_data.get("task_identifier", "")
                    if task_identifier:
                        self._record_event("background_bash_started", {"task_identifier": task_identifier, "command": command})
                        if lease_token and self._background.add_done_callback(
                            task_identifier,
                            lambda _identifier, token=lease_token: self._release_filesystem_lease(token),
                        ):
                            lease_token = ""
            finally:
                self._release_filesystem_lease(lease_token)

        elif tool_name == "read_file":
            file_path = str(tool_arguments.get("file_path", ""))
            offset = tool_arguments.get("offset", 1) or 1
            limit_raw = tool_arguments.get("limit")
            limit = int(limit_raw) if limit_raw not in (None, "") else None
            result = await asyncio.to_thread(
                file_tools.read_file, self._working_directory, file_path, int(offset), limit,
            )
            result_data = _maybe_json(result)
            # Record the canonical path and hash so edit_file/write_file can
            # reject stale edits.
            if isinstance(result_data, dict):
                sha256 = result_data.get("sha256")
                if isinstance(sha256, str):
                    self._read_files[self._canonical_file_path(file_path)] = sha256
            yield StreamEvent(
                StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=result_data,
            )

        elif tool_name == "find_files":
            pattern = str(tool_arguments.get("pattern", ""))
            result = await asyncio.to_thread(file_tools.find_files, self._working_directory, pattern)
            yield StreamEvent(
                StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=_maybe_json(result),
            )

        elif tool_name == "search_content":
            pattern = str(tool_arguments.get("pattern", ""))
            include = tool_arguments.get("include")
            include = str(include) if include else None
            search_path = tool_arguments.get("path")
            search_path = str(search_path) if search_path else None
            result = await asyncio.to_thread(
                file_tools.search_content, self._working_directory, pattern, include, search_path,
            )
            yield StreamEvent(
                StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=_maybe_json(result),
            )

        elif tool_name == "fetch_url":
            url = str(tool_arguments.get("url", ""))
            fmt = str(tool_arguments.get("format", "markdown") or "markdown")
            timeout = int(tool_arguments.get("timeout", 30) or 30)
            result = await file_tools.fetch_url(url, fmt, timeout)
            yield StreamEvent(
                StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=_maybe_json(result),
            )

        elif tool_name in ("edit_file", "write_file"):
            if self._read_only:
                deny_message = self._prompt_loader.load("read_only_denied", {"violation": "a file modification"})
                yield StreamEvent(
                    StreamEvent.Type.ERROR, id=tool_call_identifier, message=deny_message, tool=tool_name,
                )
                return
            file_path = str(tool_arguments.get("file_path", ""))
            resolved = self._canonical_file_path(file_path)
            try:
                lease_token = await self._acquire_filesystem_lease(
                    scope="file",
                    path=resolved,
                    description=f"{tool_name}: {resolved}",
                )
            except FileLeaseConflict as exception:
                yield StreamEvent(
                    StreamEvent.Type.ERROR,
                    id=tool_call_identifier,
                    code="filesystem_lease_conflict",
                    message=str(exception),
                    tool=tool_name,
                )
                return
            try:
                expected_sha256 = self._read_files.get(resolved)
                if tool_name == "edit_file":
                    old_string = tool_arguments.get("old_string", "")
                    new_string = tool_arguments.get("new_string", "")
                    if not isinstance(old_string, str) or not isinstance(new_string, str):
                        raise ValueError("old_string and new_string must be strings.")
                    replace_all = bool(tool_arguments.get("replace_all", False))
                    result = await asyncio.to_thread(
                        file_tools.edit_file,
                        self._working_directory,
                        file_path,
                        old_string,
                        new_string,
                        replace_all,
                        expected_sha256=expected_sha256,
                    )
                else:
                    content = tool_arguments.get("content", "")
                    if not isinstance(content, str):
                        content = json.dumps(content)
                    result = await asyncio.to_thread(
                        file_tools.write_file, self._working_directory, file_path, content, expected_sha256=expected_sha256,
                    )
                result_data = _maybe_json(result)
                if tool_name == "edit_file":
                    if isinstance(result_data, dict) and isinstance(result_data.get("sha256"), str):
                        self._read_files[resolved] = result_data["sha256"]
                    else:
                        self._read_files.pop(resolved, None)
                else:
                    # write_file: the model supplied the full content, so it knows
                    # the current state and can edit without re-reading.
                    self._read_files[resolved] = file_tools.content_sha256(content)
                yield StreamEvent(
                    StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=result_data,
                )
            finally:
                self._release_filesystem_lease(lease_token)

        elif tool_name == "load_skill":
            skill_name = str(tool_arguments.get("name", ""))
            all_skills = enabled_skills(
                load_skills(self._global_configuration.skill_directories_for(self._project_directory))
            )
            match = next((s for s in all_skills if s.identifier == skill_name), None)
            if match is None:
                yield StreamEvent(
                    StreamEvent.Type.ERROR,
                    id=tool_call_identifier,
                    message=f"No enabled skill named '{skill_name}'.",
                    tool=tool_name,
                )
                return
            result = json.dumps({
                "code": "skill_loaded",
                "name": match.identifier,
                "title": match.display_title,
                "path": match.path,
                "content": match.body,
            })
            yield StreamEvent(
                StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=_maybe_json(result),
            )

        elif tool_name == "ask_user":
            questions = tool_arguments.get("questions", [])
            request_identifier = f"q-{self._session_id}-{uuid.uuid4()}"
            future = asyncio.get_event_loop().create_future()
            self._pending_questions[request_identifier] = future
            yield StreamEvent(
                StreamEvent.Type.QUESTION,
                id=tool_call_identifier,
                request_id=request_identifier,
                questions=questions,
            )
            try:
                answers = await future
            finally:
                self._pending_questions.pop(request_identifier, None)
            result = json.dumps({"code": "user_answered", "answers": answers})
            yield StreamEvent(
                StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=_maybe_json(result),
            )

        elif tool_name == "call_mcp_tool":
            read_only = tool_arguments.get("read_only", True)
            risk = tool_arguments.get("risk", "low")
            if self._read_only and not read_only:
                deny_message = self._prompt_loader.load("read_only_denied", {"violation": "a mutating MCP tool call"})
                yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message=deny_message, tool=tool_name)
                return
            if not self._bypass_permissions and not read_only and risk in ("medium", "high"):
                request_identifier = f"perm-{self._session_id}-{uuid.uuid4()}"
                future = asyncio.get_event_loop().create_future()
                self._pending_permissions[request_identifier] = future
                yield StreamEvent(
                    StreamEvent.Type.PERMISSION_REQUEST,
                    id=tool_call_identifier,
                    request_id=request_identifier,
                    command=f"MCP {tool_arguments.get('server', '')}.{tool_arguments.get('tool_name', '')}",
                    justification=tool_arguments.get("justification", ""),
                    risk=risk,
                )
                allowed = await self._resolve_permission_future(request_identifier, future, "", is_bash=False)
                if not allowed:
                    yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message="MCP tool call not approved by user", tool=tool_name)
                    return
            event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

            async def on_mcp_event(event: dict[str, Any]) -> None:
                await event_queue.put(event)

            call_task = asyncio.create_task(call_mcp_tool_with_events(
                str(tool_arguments.get("server", "")),
                str(tool_arguments.get("tool_name", "")),
                _coerce_mcp_arguments(tool_arguments.get("arguments")),
                on_mcp_event,
            ))
            try:
                while True:
                    if call_task.done() and event_queue.empty():
                        break
                    get_task = asyncio.create_task(event_queue.get())
                    done, pending = await asyncio.wait(
                        {call_task, get_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if get_task in done:
                        yield StreamEvent(
                            StreamEvent.Type.MCP_EVENT,
                            id=tool_call_identifier,
                            name="call_mcp_tool",
                            server=tool_arguments.get("server", ""),
                            tool=tool_arguments.get("tool_name", ""),
                            event=get_task.result(),
                        )
                    for pending_task in pending:
                        if pending_task is get_task:
                            pending_task.cancel()
                result_data = await call_task
            except Exception as exception:
                yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message=str(exception), tool=tool_name)
                return
            yield StreamEvent(StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=result_data)

        elif tool_name in ("list_mcp_tools", "list_mcp_resources", "read_mcp_resource"):
            tool_map = {
                "list_mcp_tools": list_mcp_tools_tool,
                "list_mcp_resources": list_mcp_resources_tool,
                "read_mcp_resource": read_mcp_resource_tool,
            }
            result = await tool_map[tool_name].ainvoke(tool_arguments)
            result_data = _maybe_json(result)
            yield StreamEvent(StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=result_data)

        elif tool_name == "spawn_agent":
            child_depth = self._delegation_depth + 1
            maximum_depth = self._global_configuration.maximum_delegation_depth
            if child_depth > maximum_depth:
                yield StreamEvent(
                    StreamEvent.Type.ERROR, id=tool_call_identifier, tool=tool_name,
                    message=f"Maximum delegation depth ({maximum_depth}) reached; cannot spawn another agent.",
                )
                return

            raw_sub_agent_prompt = tool_arguments.get("prompt", "")
            sub_agent_name = tool_arguments.get("agent", self._global_configuration.default_agent)
            sub_agent_read_only = tool_arguments.get("read_only", None)
            if isinstance(sub_agent_read_only, str):
                sub_agent_read_only = sub_agent_read_only.lower() == "true"
            sub_agent_prompt = self._build_sub_agent_prompt(raw_sub_agent_prompt, sub_agent_read_only)
            spawn_step_id = new_id("agent")

            try:
                sub_configuration = self._load_sub_agent(sub_agent_name)
            except FileNotFoundError as exception:
                yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message=str(exception), tool=tool_name)
                return

            # Spawning runs the sub-agent as a related A2A task and streams its
            # activity live into the agents panel (grouped per turn). Its
            # structured deliverable (the child task) returns immediately as this
            # tool's result, so the parent can reason over it and spawn further
            # agents — peer-to-peer, with the dependency shape emerging from the
            # parent's reasoning rather than a declared graph.
            group_id = f"agents-{self._a2a_task_id or self._session_id}"
            yield StreamEvent(
                StreamEvent.Type.AGENT_GROUP_STARTED,
                group_id=group_id,
                tool_call_id=tool_call_identifier,
                steps=[{"id": spawn_step_id, "agent": sub_agent_name, "prompt": raw_sub_agent_prompt}],
            )
            self._record_event("agent_spawned", {"task_identifier": spawn_step_id, "agent": sub_agent_name, "prompt": raw_sub_agent_prompt})
            child_task = None
            if self._delegate is not None:
                async for delegated in self._delegate(
                    sub_agent_name,
                    sub_agent_prompt,
                    self._a2a_task_id,
                    sub_agent_read_only,
                    child_depth,
                    self._working_directory,
                    self._project_directory,
                ):
                    delegated_kind = delegated.get("type")
                    common = {
                        "group_id": group_id,
                        "step_id": spawn_step_id,
                        "child_task_id": delegated.get("child_task_id", ""),
                    }
                    if delegated_kind == "started":
                        yield StreamEvent(StreamEvent.Type.AGENT_STATUS, code="started", **common)
                    elif delegated_kind == "text":
                        yield StreamEvent(StreamEvent.Type.AGENT_TEXT_CHUNK, text=delegated.get("text", ""), **common)
                    elif delegated_kind == "thinking":
                        yield StreamEvent(StreamEvent.Type.AGENT_THINKING, text=delegated.get("text", ""), **common)
                    elif delegated_kind == "thinking_done":
                        yield StreamEvent(StreamEvent.Type.AGENT_THINKING_DONE, duration_ms=delegated.get("durationMs", 0), **common)
                    elif delegated_kind == "status":
                        yield StreamEvent(StreamEvent.Type.AGENT_STATUS, code=delegated.get("code", ""), **common)
                    elif delegated_kind == "tool_call":
                        yield StreamEvent(StreamEvent.Type.AGENT_TOOL_CALL, name=delegated.get("name", ""), arguments=delegated.get("arguments", {}), toolCallId=delegated.get("toolCallId", ""), **common)
                    elif delegated_kind == "tool_result":
                        yield StreamEvent(StreamEvent.Type.AGENT_TOOL_RESULT, name=delegated.get("name", ""), result=delegated.get("result"), toolCallId=delegated.get("toolCallId", ""), **common)
                    elif delegated_kind == "mcp_event":
                        yield StreamEvent(StreamEvent.Type.AGENT_MCP_EVENT, toolCallId=delegated.get("toolCallId", ""), event=delegated.get("event", {}), **common)
                    elif delegated_kind == "error":
                        yield StreamEvent(
                            StreamEvent.Type.AGENT_TOOL_RESULT,
                            name=delegated.get("name", "") or "unknown",
                            result={"code": "tool_error", "message": delegated.get("message", "Unknown error")},
                            toolCallId=delegated.get("toolCallId", ""),
                            **common,
                        )
                    elif delegated_kind == "done":
                        child_task = delegated.get("task")
                        yield StreamEvent(StreamEvent.Type.AGENT_DONE, task=child_task, **common)
            else:
                runner = SubAgentRunner(
                    agent_configuration=sub_configuration,
                    global_configuration=self._global_configuration,
                    task_identifier=spawn_step_id,
                    prompt=sub_agent_prompt,
                    stream_progress=self._agent_configuration.stream_agent_progress,
                    read_only_override=sub_agent_read_only,
                    working_directory=self._working_directory,
                    project_directory=self._project_directory,
                )
                final_text = ""
                common = {"group_id": group_id, "step_id": spawn_step_id, "child_task_id": ""}
                async for event in runner.run_stream(always_yield_text=True):
                    if event.type == StreamEvent.Type.TEXT_CHUNK:
                        yield StreamEvent(StreamEvent.Type.AGENT_TEXT_CHUNK, text=event.data.get("text", ""), **common)
                    elif event.type == StreamEvent.Type.THINKING:
                        yield StreamEvent(StreamEvent.Type.AGENT_THINKING, text=event.data.get("text", ""), **common)
                    elif event.type == StreamEvent.Type.THINKING_DONE:
                        yield StreamEvent(StreamEvent.Type.AGENT_THINKING_DONE, duration_ms=event.data.get("duration_ms", 0), **common)
                    elif event.type == StreamEvent.Type.STATUS:
                        yield StreamEvent(StreamEvent.Type.AGENT_STATUS, code=event.data.get("code", ""), **common)
                    elif event.type == StreamEvent.Type.TOOL_CALL:
                        yield StreamEvent(StreamEvent.Type.AGENT_TOOL_CALL, name=event.data.get("name", ""), arguments=event.data.get("arguments", {}), toolCallId=event.data.get("id", ""), **common)
                    elif event.type == StreamEvent.Type.TOOL_RESULT:
                        yield StreamEvent(StreamEvent.Type.AGENT_TOOL_RESULT, name=event.data.get("name", ""), result=event.data.get("result"), toolCallId=event.data.get("id", ""), **common)
                    elif event.type == StreamEvent.Type.MCP_EVENT:
                        yield StreamEvent(StreamEvent.Type.AGENT_MCP_EVENT, toolCallId=event.data.get("id", ""), event=event.data.get("event", {}), **common)
                    elif event.type == StreamEvent.Type.ERROR:
                        yield StreamEvent(
                            StreamEvent.Type.AGENT_TOOL_RESULT,
                            name=event.data.get("tool", "") or "unknown",
                            result={"code": "tool_error", "message": event.data.get("message", "Unknown error")},
                            toolCallId=event.data.get("id", ""),
                            **common,
                        )
                    elif event.type == StreamEvent.Type.DONE:
                        final_text = event.data.get("text", final_text)
                child_task = serialize_task(build_task(spawn_step_id, sub_agent_name, TaskState.completed, final_text))
                yield StreamEvent(StreamEvent.Type.AGENT_DONE, task=child_task, **common)

            result_payload = child_task or {"code": "empty_response", "message": "Sub-agent produced no task."}
            yield StreamEvent(StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=result_payload)

        elif tool_name == "set_tasks":
            task_definitions = tool_arguments.get("tasks", [])
            identifiers = self._task_manager.add_tasks(task_definitions)
            result_message = f"Created the tasks {', '.join(identifiers)}"
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
                result_message = f"Updated the tasks {', '.join(updated_ids)}"
            else:
                result_message = "No matching tasks found."
            updated_tasks = [
                task for task in self._task_manager.to_dict_list()
                if task["identifier"] in updated_ids
            ]
            yield StreamEvent(
                StreamEvent.Type.TASKS_UPDATED,
                id=tool_call_identifier,
                tasks=updated_tasks,
                result_message=result_message,
            )

        elif tool_name == "update_goal":
            status = tool_arguments.get("status", "active")
            goal = str(tool_arguments.get("goal", "")).strip()
            if status == "active":
                if not goal:
                    result = {
                        "code": "goal_update_error",
                        "message": "A non-empty goal is required when status is 'active'.",
                    }
                else:
                    self._active_goal = goal
                    result = {
                        "code": "goal_active",
                        "goal": self._active_goal,
                    }
                    self._record_event("goal_updated", result)
            elif status in ("satisfied", "cleared"):
                previous_goal = self._active_goal
                self._active_goal = ""
                result = {
                    "code": f"goal_{status}",
                    "previous_goal": previous_goal,
                }
                self._record_event("goal_updated", result)
            else:
                result = {
                    "code": "goal_update_error",
                    "message": "status must be one of 'active', 'satisfied', or 'cleared'.",
                }
            yield StreamEvent(StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=result)

        elif tool_name == "open_preview":
            if self._is_sub_agent:
                yield StreamEvent(
                    StreamEvent.Type.ERROR,
                    id=tool_call_identifier,
                    tool=tool_name,
                    code="sub_agent_preview_denied",
                    message="Sub-agents cannot open previews. Return findings only as text for the parent agent.",
                )
                return
            raw_url = str(tool_arguments.get("url", "")).strip()
            if not raw_url:
                yield StreamEvent(
                    StreamEvent.Type.ERROR, id=tool_call_identifier, tool=tool_name,
                    code="empty_preview", message="open_preview requires a url or file path.",
                )
                return
            # An http(s) URL is previewed as-is; anything else is treated as a local
            # file path (a file:// URL, an absolute path, or one relative to the
            # working directory) and must resolve to an existing file on disk.
            lowered = raw_url.lower()
            if lowered.startswith(("http://", "https://")):
                source, is_file = raw_url, False
            else:
                candidate = raw_url[len("file://"):] if lowered.startswith("file://") else raw_url
                path = Path(candidate).expanduser()
                if not path.is_absolute():
                    path = Path(self._working_directory or Path.cwd()) / path
                path = path.resolve()
                if not path.is_file():
                    yield StreamEvent(
                        StreamEvent.Type.ERROR, id=tool_call_identifier, tool=tool_name,
                        code="preview_file_not_found",
                        message=f"No file to preview at {path}. Write the file first, then preview it.",
                    )
                    return
                source, is_file = str(path), True
            result = build_web_preview_result(
                source,
                is_file=is_file,
                title=str(tool_arguments.get("title", "Preview")),
                height=tool_arguments.get("height", 0),
                artifact_id=str(tool_arguments.get("artifact_id", "")),
                artifact_update_mode=str(tool_arguments.get("artifact_update_mode", "append")),
                artifact_target_id=str(tool_arguments.get("artifact_target_id", "")),
                summary=str(tool_arguments.get("summary", "")),
            )
            yield StreamEvent(StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=result)

        elif tool_name == "web_search":
            background_token = bind_background_jobs(self._background)
            try:
                result = await web_search_tool.ainvoke(tool_arguments)
            finally:
                unbind_background_jobs(background_token)
            result_data = _maybe_json(result)
            if isinstance(result_data, dict) and result_data.get("code") == "web_search_started":
                # Attach the "don't poll/read_task this" guidance from a prompt
                # template, keeping user-facing wording out of the tool code.
                result_data["note"] = self._prompt_loader.load("web_search_started_note", {})
            yield StreamEvent(StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=result_data)

        elif tool_name == "read_task":
            requested_task_id = tool_arguments.get("task_id", "")
            # A web_search/background-bash handle ("search-…"/"bg-…") is not an A2A
            # task — its results are delivered automatically, never read. Catch the
            # mistake with a redirect instead of a bare "task_not_found" that just
            # invites the model to retry the same wrong poll.
            background_kind = _background_handle_kind(requested_task_id)
            if self._task_reader is None:
                result = {"code": "read_task_unavailable", "message": "Reading tasks is not available in this context."}
            elif background_kind is not None:
                message = self._prompt_loader.load(
                    "read_task_background_handle",
                    {"task_id": requested_task_id, "kind": background_kind},
                )
                result = {"code": "not_a_readable_task", "task_id": requested_task_id, "message": message}
            else:
                task = await self._task_reader(requested_task_id)
                if task is None:
                    result = {"code": "task_not_found", "task_id": requested_task_id}
                else:
                    result = task
            yield StreamEvent(StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=result)

        else:
            yield StreamEvent(
                StreamEvent.Type.ERROR, id=tool_call_identifier,
                message=f"Unknown tool '{tool_name}'", tool=tool_name,
            )

    def get_execution_history(self) -> list[dict]:
        return self._execution_history
