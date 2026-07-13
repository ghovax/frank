import asyncio
import json
import os
import platform
import shlex
import time
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from enum import Enum
import hashlib
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Literal, Optional, cast

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.ai import add_ai_message_chunks
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, SecretStr, ValidationError

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
from langchain_core.language_models.chat_models import BaseChatModel
from harness.core.litellm_model import ChatLiteLLMModel
from harness.core.codex_model import ChatCodexModel
from harness.core.file_leases import FileLeaseConflict, FileLeaseManager
from harness.core.models import find_model, resolve_litellm
from harness.locations.resolver import LocationAddress, executor_for, location_uri_for
from harness.tools.tools import (
    bash as bash_tool,
    web_search as web_search_tool,
    spawn_agent as spawn_tool,
    read_task as read_task_tool,
    set_tasks as set_tasks_tool,
    update_tasks as update_tasks_tool,
    update_goal as update_goal_tool,
    open_artifact as open_artifact_tool,
    build_open_artifact_result,
    artifact_kind_for,
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
    download_file as download_file_tool,
    computer as computer_tool,
    browser as browser_tool,
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
    bind_tool_call_id,
    unbind_tool_call_id,
)

from harness.tools import file_tools

from harness.core.handoff import (
    build_task,
    serialize_task,
)
from harness.core.environment import probe_local_environment, probe_user_context
from harness.core.tool_policy import (
    BashAllowRule,
    BashPermissionDecision,
    CallExecutionPolicy,
    ResolvedLocation,
    ToolLocationError,
    _LOCATION_TOOLS,
)
from harness.core.memories import load_memories, memories_payload
from harness.core.skills import load_skills, enabled_skills, skills_for_agent, skills_payload
from harness.core.instructions import load_instructions
from harness.core.events import ModelToolResult, ToolMetadata, ToolStatus, TurnContext, tool_status_from_result
from harness.identifiers import new_id


class Observation(BaseModel):
    """One entry in the conversation-memory log (Observational Memory)."""

    category: Literal["decision", "fact", "artifact", "goal", "open"] = Field(
        description=(
            "decision — a choice or approach the agent committed to; "
            "fact — something learned about the codebase, system, or world; "
            "artifact — a file/path/resource created or modified (record the exact path); "
            "goal — the user's objective, preference, or constraint; "
            "open — an unfinished thread or an agreed next step."
        )
    )
    detail: str = Field(
        description="A terse, information-dense note. Record outcomes and state, and keep "
        "concrete identifiers (paths, ids, names, commands, numbers) exact."
    )


class ObservationBatch(BaseModel):
    """The structured memory the Observer/Reflector emits as a tool call — so the shape
    is guaranteed by the model's tool-calling, never scraped from free text."""

    observations: list[Observation] = Field(default_factory=list)


# Sentinel returned by ``_stream_next`` when an async iterator is exhausted, so a
# stream read can be raced against an abort inside a Task without a
# StopAsyncIteration propagating through it (which asyncio mishandles).
_STREAM_EXHAUSTED = object()


async def _stream_next(iterator: AsyncIterator) -> Any:
    """Return the next item from ``iterator``, or ``_STREAM_EXHAUSTED`` when it is
    done. Wrapped so the model stream can be driven one read at a time and each read
    raced against the abort event — a Stop then interrupts the turn even while it is
    parked on the network awaiting the next token."""
    try:
        return await iterator.__anext__()
    except StopAsyncIteration:
        return _STREAM_EXHAUSTED


def build_chat_model(
    model_identifier: str,
    global_configuration: "GlobalConfiguration",
    agent_configuration: "AgentConfiguration",
) -> BaseChatModel:
    """Build the chat model for a provider-qualified model id.

    Almost every provider flows through one ``ChatLiteLLMModel`` (LiteLLM owns each
    provider's auth, base URL, request format, and reasoning normalization). A
    bare model id with no provider prefix defaults to the OpenCode gateway and is
    qualified as ``opencode/<model>``. The one exception is the experimental
    ``chatgpt`` subscription provider, which is not a LiteLLM route at all: it uses
    its own ``ChatCodexModel``, reading its OAuth token from the shared token store
    (no api_key/api_base) and calling Codex's Responses endpoint directly."""
    if "/" not in model_identifier:
        model_identifier = f"opencode/{model_identifier}"
    provider_identifier, model_suffix = model_identifier.split("/", 1)
    if provider_identifier == "chatgpt":
        catalog_entry = find_model(model_identifier)
        return ChatCodexModel(
            model=model_suffix,
            reasoning_effort=agent_configuration.reasoning_effort,
            context_length=catalog_entry.context_length if catalog_entry else 0,
        )
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
        ERROR = "error"
        DENIED_INJECTION = "denied_injection"
        # A spawn_agent invocation begins a child group. RELAYED carries a child's own
        # (unified) event with its path already prefixed by this agent's segment — a
        # agent is just an agent at a deeper path, so there is no separate
        # agent event vocabulary to translate through.
        GROUP_STARTED = "group_started"
        RELAYED = "relayed"
        STEERING = "steering"
        COMPACTION_STARTED = "compaction_started"
        COMPACTION_DONE = "compaction_done"

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
    excerpt = result[:MAXIMUM_MODEL_RESULT_CHARS]
    parsed = _maybe_json(result)
    if isinstance(parsed, dict):
        payload = {
            **parsed,
            "truncated": True,
            "full_output_file": str(output_path),
            "output_excerpt": excerpt,
        }
        for large_key in ("output", "content", "summary", "results"):
            if large_key in payload:
                payload.pop(large_key, None)
        return json.dumps(payload, ensure_ascii=False)
    return json.dumps({
        "code": code,
        "truncated": True,
        "full_output_file": str(output_path),
        "output_excerpt": excerpt,
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


_MODEL_PROMPT_LOADER = PromptLoader(Path(__file__).parent / "prompts")


def _model_visible_tool_result(
    content: str, metadata: dict[str, Any], status: str, code: str | None = None, *, kind: str = "tool_result",
) -> str:
    """The canonical model-facing tool result: a one-line JSON metadata header, a blank
    line, then the tool's raw output body. The body is delivered **as-is** — prose stays
    prose (never re-encoded into an escaped JSON string), structured output stays JSON — so
    a long log or a fetched page reads cleanly instead of collapsing onto one escaped line.
    Used for both an inline ToolMessage (``kind="tool_result"``) and a background-completion
    system message (``kind="background_result"``). Mirrors :class:`events.ModelToolResult`."""
    header: dict[str, Any] = {
        "kind": kind,
        "tool_name": metadata.get("tool_name", ""),
        "tool_call_id": metadata.get("tool_call_id", ""),
        "status": status,
        "code": code,
    }
    for key in ("started_at", "completed_at", "duration_ms", "background_task_identifier"):
        value = metadata.get(key)
        if value is not None:
            header[key] = value
    return _MODEL_PROMPT_LOADER.load("model_tool_result", {
        "header": json.dumps(header, ensure_ascii=False),
        "content": content,
    })


def _model_result_status(content: str, *, ok: bool, backgrounded: bool) -> tuple[str, str | None]:
    """The (status, code) for a model-facing tool result. A failed tool is an error; a
    backgrounded one is still running; otherwise fall back to the payload's own code."""
    parsed = _maybe_json(content)
    code = parsed.get("code") if isinstance(parsed, dict) else None
    if not ok:
        return ToolStatus.ERROR.value, code
    if backgrounded:
        return ToolStatus.RUNNING.value, code
    return tool_status_from_result(parsed).value, code


def _spawned_agent_report(child_task: dict | None, agent_name: str) -> str:
    """The agent's deliverable — its result artifact(s), exactly as the A2A
    layer already serialized them — as the JSON injected into the parent's context.
    Only the deliverable is passed back (never the agent's progress, transcript,
    or task scaffolding), and the artifact JSON is passed through as-is rather than
    re-extracted or re-joined by hand. Falls back to a short note when the agent
    produced no artifact."""
    artifacts = child_task.get("artifacts") if isinstance(child_task, dict) else None
    if artifacts:
        return json.dumps({"code": "agent_report", "agent": agent_name, "artifacts": artifacts}, ensure_ascii=False)
    return json.dumps(
        {"code": "agent_no_report", "agent": agent_name,
         "message": f"The '{agent_name}' agent finished without producing a report."},
        ensure_ascii=False,
    )


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


def _container_origins(annotation: Any) -> set:
    """The container origins (``list`` and/or ``dict``) an annotation can be, seen through
    Optional/Union wrappers. Used to decide whether a string argument should be JSON-parsed."""
    import types as types_module
    import typing

    origins: set = set()

    def visit(current: Any) -> None:
        origin = typing.get_origin(current)
        if origin in (list, dict):
            origins.add(origin)
        elif origin is typing.Union or origin is getattr(types_module, "UnionType", None):
            for argument in typing.get_args(current):
                visit(argument)

    visit(annotation)
    return origins


def _coerce_structured_arguments(schema: Any, arguments: dict) -> dict:
    """Models frequently serialize array/object tool arguments as a JSON *string* rather than a
    native value (e.g. ``"[\\"a\\"]"`` for a ``list[str]`` field). For any field the schema types
    as a list or dict, parse a string that is well-formed JSON of that shape and use the parsed
    value, so a stringified-but-valid argument is accepted by both validation and dispatch
    instead of being rejected by the typed field. Returns a new dict; values that do not apply
    pass through unchanged."""
    if not isinstance(arguments, dict):
        return arguments
    model_fields = getattr(schema, "model_fields", {})
    coerced = dict(arguments)
    for name, value in arguments.items():
        if not isinstance(value, str):
            continue
        field = model_fields.get(name)
        if field is None:
            continue
        origins = _container_origins(field.annotation)
        if not origins:
            continue
        text = value.strip()
        if not text or text[0] not in "[{":
            continue
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            continue
        if (list in origins and isinstance(parsed, list)) or (dict in origins and isinstance(parsed, dict)):
            coerced[name] = parsed
    return coerced


def _screenshot_data_uri(path: str) -> str:
    """Read a captured PNG into a data URI for the model_image side channel, then remove
    the temp file. Empty string on any failure — a screenshot is best-effort."""
    import base64
    from contextlib import suppress
    try:
        with open(path, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("ascii")
    except OSError:
        return ""
    finally:
        with suppress(OSError):
            os.remove(path)
    return f"data:image/png;base64,{encoded}"


def _build_tools(
    agent_configuration: AgentConfiguration,
    global_configuration: GlobalConfiguration,
    *,
    is_agent: bool = False,
) -> list[BaseTool]:
    available = [
        bash_tool,
        read_file_tool,
        find_files_tool,
        search_content_tool,
        edit_file_tool,
        write_file_tool,
        fetch_url_tool,
        download_file_tool,
        load_skill_tool,
        web_search_tool,
        set_tasks_tool,
        update_tasks_tool,
        update_goal_tool,
        read_task_tool,
    ]
    if not is_agent:
        available.append(open_artifact_tool)
        available.append(ask_user_tool)
    if agent_configuration.tools.spawn_agent.enabled:
        available.append(spawn_tool)
    # The computer-use tool controls the whole machine, so it is opt-in: added only when
    # the user has enabled it in Settings (which also gates the Accessibility grant flow).
    if global_configuration.computer_control.enabled:
        available.append(computer_tool)
        available.append(browser_tool)
    if global_configuration.mcp.enabled_servers():
        available.extend([
            list_mcp_tools_tool,
            call_mcp_tool_tool,
            list_mcp_resources_tool,
            read_mcp_resource_tool,
        ])
    return available


class AgentRunner:
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
            is_agent=True,
        )
        # An explicit override (from the spawning call or step)
        # wins over the agent profile's own permission_mode.
        if read_only_override is not None:
            self._runtime.set_read_only(read_only_override)

    async def run_stream(self, always_yield_text: bool = False) -> AsyncIterator[StreamEvent]:
        """Yield each event as the agent produces it, guaranteeing the run
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
    _GOAL_CONTINUATION_LIMIT = 8
    # Agents (delegation depth > 0) get a tighter iteration budget than the
    # top-level chat agent so a looping agent fails fast instead of burning
    # the full budget on redundant calls.
    _AGENT_MAXIMUM_ITERATIONS = 512
    # Context compaction is Observational Memory (Observer/Reflector). Its thresholds
    # and on/off switch live in GlobalConfiguration.compaction, not as constants here.

    def __init__(
        self,
        agent_configuration: AgentConfiguration,
        global_configuration: GlobalConfiguration,
        pending_permissions: Optional[dict[str, asyncio.Future]] = None,
        pending_questions: Optional[dict[str, asyncio.Future]] = None,
        on_record_event: Optional[Callable[..., Any]] = None,
        on_record_message: Optional[Callable[..., Any]] = None,
        session_id: str = "",
        conversation: Optional[list] = None,
        working_directory: str = "",
        project_directory: str = "",
        is_agent: bool = False,
        file_lease_manager: FileLeaseManager | None = None,
        locations: list[dict] | None = None,
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
        # The project's locations the agent may address per tool call (keyed by URI, and
        # by name for friendlier errors). When none are supplied (agents built without
        # an explicit set, or a bare runtime), a single local location is synthesized from
        # the working directory so the single-location default still works.
        self._locations: dict[str, ResolvedLocation] = {}
        self._locations_by_name: dict[str, ResolvedLocation] = {}
        self._build_locations(locations, permission_mode_default=agent_configuration.permission_mode)

        effective_model = agent_configuration.model_identifier
        if not effective_model:
            raise ValueError(
                f"Agent '{agent_configuration.identifier}' must configure both provider and model."
            )
        self._effective_model_identifier = effective_model

        self._llm = build_chat_model(
            effective_model, global_configuration, agent_configuration
        )

        self._is_agent = is_agent
        self._file_lease_manager = file_lease_manager
        self._tools = _build_tools(
            agent_configuration,
            global_configuration,
            is_agent=is_agent,
        )
        # Concrete tools are bound natively — the provider sees each tool's real
        # JSON schema and can constrain argument decoding to it, and it emits
        # several tool calls in one response when work is parallel. (The old
        # single `query` dispatch envelope hid every schema behind `list[Any]`.)
        # Parallel tool calls are the DEFAULT on every provider this harness
        # routes to (OpenAI, Anthropic, Gemini, Mistral, the OpenAI-compatible
        # family, …), so no `parallel_tool_calls` parameter is sent: LiteLLM
        # forwards it verbatim to openai-compatible custom gateways — most of
        # this harness's provider matrix — where a non-conforming server (or an
        # o-series model) rejects it. What actually preserves parallelism is
        # never forcing `tool_choice` and keeping each turn's tool results in
        # one contiguous block (see the turn loop).
        self._tool_schemas: dict[str, Any] = {tool.name: tool.args_schema for tool in self._tools}
        self._bound_llm = self._llm.bind_tools(self._tools)
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
        # Files the model has read this session, keyed by (location uri, resolved
        # path) with the content hash from the last read — the uri disambiguates a
        # same-named path on two hosts. Mutating tools compare against this so
        # stale line numbers cannot overwrite externally changed content.
        self._read_files: dict[tuple[str, str], str] = {}
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
        # A separate bucket for the combined spend of agents this agent spawns. They
        # run in their own context (only their deliverable returns here), so their tokens
        # are a distinct cost surfaced separately, never mixed into the context fill.
        self._agent_token_usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
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
        # When set, agents (spawn_agent calls) are invoked
        # through this delegate — an A2A call to the target agent's served
        # endpoint — instead of being run in-process. Bound to the A2A context.
        self._delegate: Optional[Callable] = None
        # Cancels a running agent's own A2A task on Stop (async callback injected by
        # the executor), plus the live agent task ids to cancel: {child_task_id -> agent}.
        self._cancel_delegated: Optional[Callable] = None
        self._active_delegations: dict[str, str] = {}
        self._a2a_task_id: str = ""
        # Reads another A2A task (sibling/agent) by id from the shared store,
        # so context-aware agents can coordinate. Injected by the executor.
        self._task_reader: Optional[Callable] = None
        # Enqueues a shadow-git capture of what a write-ish tool call produced (called after
        # edit/write/bash and on open_artifact). Injected by the executor; non-blocking and
        # best-effort so the runtime never waits on git or touches the database directly.
        self._artifact_capture: Optional[Callable] = None
        self._steering_messages: asyncio.Queue[str] = asyncio.Queue()
        self._steering_available = asyncio.Event()
        self._active_tool_tasks: dict[str, asyncio.Task] = {}
        # Results from background agent tasks, keyed by spawn_step_id.
        # Populated when an agent runs in the background; can be retrieved
        # later via read_task or the agents panel.
        self._background_agent_results: dict[str, dict | None] = {}
        # Live activity from non-blocking spawned agents. spawn_agent returns
        # immediately (the agent runs as a background job); the agent's
        # streamed events land here and the turn loop drains them so the agents
        # panel updates while the parent keeps working. Whatever is still queued
        # when the parent goes idle drains on the next (wake) turn.
        self._spawned_agent_events: "asyncio.Queue[StreamEvent]" = asyncio.Queue()
        # The latest call's context occupancy (prompt + completion) and the model's
        # window, tracked from usage so auto-compaction can fire before the next call
        # would overflow. Zero until the first call reports usage.
        self._latest_context_tokens: int = 0
        self._context_window: int = 0

    def _build_locations(self, locations: list[dict] | None, *, permission_mode_default: str) -> None:
        """Build the resolved-location map from the project's location records. Each entry
        carries an executor (local subprocess or multiplexed SSH) and its effective policy."""
        entries = locations or []
        if not entries:
            # No project locations supplied — synthesize a single local location at the
            # working directory, so a bare/agent runtime still has exactly one location
            # (and the single-location default applies).
            entries = [{
                "name": "local",
                "kind": "local",
                "base_directory": self._working_directory,
                "permission_mode": permission_mode_default,
            }]
        for entry in entries:
            kind = entry.get("kind", "local")
            base_directory = str(entry.get("base_directory") or self._working_directory)
            host_alias = str(entry.get("host_alias") or "")
            address = LocationAddress(kind=kind, base_directory=base_directory, host_alias=host_alias)
            uri = str(entry.get("uri") or location_uri_for(address))
            resolved = ResolvedLocation(
                uri=uri,
                name=str(entry.get("name") or "location"),
                kind=kind,
                base_directory=base_directory,
                executor=executor_for(address),
                permission_mode=str(entry.get("permission_mode") or permission_mode_default),
            )
            self._locations[uri] = resolved
            self._locations_by_name[resolved.name] = resolved

    def _resolve_location(self, location_value: str | None) -> ResolvedLocation:
        """Resolve a tool call's ``location`` (a URI, or a location name) to its executor +
        policy. Omitted defaults to the project's local filesystem — so a call never has to
        repeat `location`, and an omission can never silently run on a remote host; the model
        passes `location` only to target a non-default (remote) one. An unknown value errors."""
        if not location_value:
            if len(self._locations) == 1:
                return next(iter(self._locations.values()))
            # Default an omitted location to the local filesystem, so an omission is never
            # accidentally executed on a remote host.
            local = next((location for location in self._locations.values() if location.kind == "local"), None)
            if local is not None:
                return local
            # No local location to fall back to (every location is remote) — require an
            # explicit choice rather than picking a remote host on the model's behalf.
            names = ", ".join(sorted(self._locations_by_name)) or "(none configured)"
            raise ToolLocationError(
                f"This project has only remote locations and no local default — specify `location` (one of: {names})."
            )
        if location_value in self._locations:
            return self._locations[location_value]
        if location_value in self._locations_by_name:
            return self._locations_by_name[location_value]
        names = ", ".join(sorted(self._locations_by_name)) or "(none configured)"
        raise ToolLocationError(f"Unknown location {location_value!r}. Available: {names}.")

    def _call_policy(self, location: "ResolvedLocation | None") -> CallExecutionPolicy:
        """The execution policy for one tool call. In the projects model
        permission_mode is per-location, so a location with an explicit mode of its
        own governs its calls; a location running on the agent's default mode uses
        the runtime's live flags (which carry user toggles and agent overrides).
        A runtime-level read-only override is always a floor — a read-only agent
        stays read-only at every location. Returned as a value and threaded through
        the call, never written to shared state, so concurrent calls to different
        locations cannot cross policies."""
        if location is None:
            return CallExecutionPolicy(
                location=None,
                working_directory=self._working_directory,
                read_only=self._read_only,
                bypass_permissions=self._bypass_permissions,
                auto_permissions=self._auto_permissions,
            )
        mode = location.permission_mode
        if mode == self._agent_configuration.permission_mode:
            read_only = self._read_only
            bypass = self._bypass_permissions and not read_only
            auto = self._auto_permissions and not read_only
        else:
            read_only = self._read_only or mode == "read_only"
            bypass = not read_only and mode == "bypass"
            auto = not read_only and mode == "auto"
        working_directory = self._working_directory if location.is_remote else location.base_directory
        return CallExecutionPolicy(
            location=location,
            working_directory=working_directory,
            read_only=read_only,
            bypass_permissions=bypass,
            auto_permissions=auto,
        )

    def _canonical_working_directory(self, working_directory: str | None = None) -> str:
        return str(Path(working_directory or self._working_directory or Path.home()).expanduser().resolve(strict=False))

    async def _acquire_filesystem_lease(
        self, *, scope: str, path: str, description: str, working_directory: str | None = None
    ) -> str:
        if self._file_lease_manager is None:
            return ""
        return await self._file_lease_manager.acquire(
            owner_session_id=self._session_id,
            scope=scope,
            path=path,
            working_directory=self._canonical_working_directory(working_directory),
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
        status, code = _model_result_status(capped_result, ok=True, backgrounded=False)
        self._conversation.append(self._harness_note_message(
            _model_visible_tool_result(
                capped_result, metadata, status, code, kind="background_result",
            ),
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
        # with the model's context window, it drives the context-fill indicator and
        # the auto-compaction check (see _should_compact).
        model = getattr(self, "_llm", None)
        context_window = model.context_window() if model is not None else 0
        self._latest_context_tokens = input_tokens + output_tokens
        self._context_window = context_window
        return StreamEvent(
            StreamEvent.Type.USAGE,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cache_read_tokens=cache_read,
            reasoning_tokens=reasoning,
            context_window=context_window,
            cumulative=dict(self._token_usage),
            agents=dict(self._agent_token_usage),
        )

    def _add_agent_usage(self, input_tokens: int, output_tokens: int) -> None:
        """Fold one agent model call's spend into the separate agent bucket. Each
        relayed child USAGE reports its per-call figures, so summing them totals every
        agent's usage without double-counting cumulative snapshots."""
        self._agent_token_usage["input_tokens"] += input_tokens
        self._agent_token_usage["output_tokens"] += output_tokens
        self._agent_token_usage["total_tokens"] += input_tokens + output_tokens
        self._agent_token_usage["model_calls"] += 1

    @property
    def agent_name(self) -> str:
        return self._agent_configuration.identifier

    @property
    def effective_model_identifier(self) -> str:
        return self._effective_model_identifier

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
        # Stop tears down the live turn: signal the loop to end, kill every
        # foreground tool still running (a synchronous bash, a search awaited
        # inline), but leave detached background work — anything the model
        # explicitly sent to the background — running, so a long build/scan the
        # user chose to background is not collateral of stopping the turn.
        self._abort_event.set()
        self._background.cancel_foreground()
        for task in list(self._active_tool_tasks.values()):
            task.cancel()
        # Cancelling the delegation driver (cancel_foreground) only stops our *wait* on a
        # agent; its own A2A task keeps running. Reap those too, so Stop truly ends an
        # in-flight agent. Detached background work (bash background=true) is untouched.
        if self._cancel_delegated is not None:
            for child_task_id, agent_name in list(self._active_delegations.items()):
                asyncio.create_task(self._cancel_delegated(agent_name, child_task_id))

    def abort_tool(self, tool_call_identifier: str) -> bool:
        task = self._active_tool_tasks.get(tool_call_identifier)
        aborted = False
        if task is not None and not task.done():
            task.cancel()
            aborted = True
        return self._background.cancel_by_tool_call(tool_call_identifier) or aborted

    def background_snapshots(self) -> list[dict[str, Any]]:
        return self._background.active_snapshots()

    def send_tool_to_background(self, tool_call_identifier: str) -> bool:
        """Push a still-blocking foreground shell command to the background on the
        user's behalf: it keeps running detached and the turn continues with a
        "started" placeholder, exactly as if the model had backgrounded it."""
        return self._background.request_background(tool_call_identifier)

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
        """Install the A2A delegate used to invoke agents as related tasks."""
        self._delegate = delegate

    def set_cancel_delegated(self, cancel_delegated: Callable) -> None:
        """Install the callback that cancels a running agent's A2A task on Stop."""
        self._cancel_delegated = cancel_delegated

    def set_a2a_task_id(self, task_id: str) -> None:
        """Record the A2A task id of the current turn so delegated agent
        tasks can reference it as their parent."""
        self._a2a_task_id = task_id

    def set_task_reader(self, task_reader: Callable) -> None:
        """Install the reader used by the read_task tool to fetch sibling/agent
        A2A tasks from the shared store."""
        self._task_reader = task_reader

    def set_artifact_capture(self, artifact_capture: Callable) -> None:
        """Install the callback that enqueues a shadow-git capture after a write-ish tool
        call (edit/write/bash) and on open_artifact. Non-blocking and best-effort."""
        self._artifact_capture = artifact_capture

    def _capture_written_artifacts(
        self, resolved_location: "ResolvedLocation", *, changed_absolute_paths: list[str] | None,
        tool_call_id: str, message: str, mode: str = "track",
        original_contents: dict[str, str] | None = None, surface: dict | None = None,
    ) -> None:
        """Fire-and-forget a capture for what a tool call wrote. ``mode="track"`` versions
        the named paths; ``mode="recheck"`` (after bash) restages only already-tracked files.
        Swallows all errors — a versioning hiccup must never break the agent's turn."""
        if self._artifact_capture is None or self._is_agent:
            return
        try:
            self._artifact_capture(
                context_id=self._session_id,
                location_uri=resolved_location.uri,
                executor=resolved_location.executor,
                base_directory=resolved_location.base_directory,
                changed_absolute_paths=changed_absolute_paths,
                mode=mode,
                original_contents=original_contents,
                tool_call_id=tool_call_id,
                message=message,
                surface=surface,
            )
        except Exception:
            pass

    def _artifact_surface_id(self, key: str) -> str:
        """A stable surface id derived from the session + a key (a file path or URL), so
        re-opening the same target reuses one tab without any database lookup."""
        return "artifact-" + hashlib.sha256(f"{self._session_id}:{key}".encode("utf-8")).hexdigest()[:16]

    def set_delegation_depth(self, depth: int) -> None:
        """Record how many delegation hops led to this runtime, so it can refuse
        to delegate past the configured maximum depth."""
        self._delegation_depth = depth

    def _evaluate_bash_permission(self, command: str, *, bypass: bool | None = None) -> str:
        effective_bypass = self._bypass_permissions if bypass is None else bypass
        if effective_bypass:
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

    def _locations_summary(self) -> list[dict]:
        """The project's locations as the model sees them: the `location` URI to pass,
        plus name/kind/base_directory/permission so it can choose the right one per tool call."""
        return [
            {
                "location": resolved.uri,
                "name": resolved.name,
                "kind": resolved.kind,
                "base_directory": resolved.base_directory,
                "permission_mode": resolved.permission_mode,
            }
            for resolved in self._locations.values()
        ]

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
                "is_agent": self._is_agent,
                # The project's locations. Filesystem/shell tools take a `location` (its
                # URI); it is required when there is more than one, optional when one.
                "locations": self._locations_summary(),
            })
            agent_context = "This agent is initialized as the main orchestrator agent."
            if self._is_agent:
                agent_context = self._prompt_loader.load("agent_context", {})
            # The opt-in user-context section is its own template, rendered into the prompt's
            # `user_environment` slot only when enabled and the probe found something — so the
            # section (heading and all) simply is not there when off.
            user_environment = ""
            user_context = getattr(self._global_configuration, "user_context", None)
            if user_context is not None and user_context.enabled:
                user_context_snapshot = probe_user_context()
                if user_context_snapshot not in ("", "{}"):
                    user_environment = self._prompt_loader.load(
                        "user_context", {"user_context_snapshot": user_context_snapshot}
                    )
            # The computer/browser tools are opt-in, so their guidance (what each is for, and
            # to pick the right one rather than force one) only enters the prompt when they do.
            computer_control_guidance = ""
            if self._global_configuration.computer_control.enabled:
                computer_control_guidance = self._prompt_loader.load("computer_control_guidance", {})
            self._cached_system_prompt = self._prompt_loader.load("system_prompt", {
                "system_prompt": self._system_prompt,
                "context": context_json,
                "system_environment": probe_local_environment(),
                "user_environment": user_environment,
                "instructions": load_instructions(self._project_directory),
                "skills": json.dumps(skills_payload(agent_skills)),
                "memories": json.dumps(memories_payload(memories)),
                "agent_context": agent_context,
                "computer_control_guidance": computer_control_guidance,
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
            "is_agent": self._is_agent,
        }
        turn_reminders = self._prompt_loader.load("turn_reminders", {
            "turn_metadata": json.dumps(turn_metadata, sort_keys=True),
        }).strip()
        # One structured object per turn (was a pile of separate newline-joined JSON
        # blobs). Empty goal/tasks are omitted so the model isn't fed noise.
        context = TurnContext(
            pwd=self._working_directory or str(Path.cwd()),
            active_goal=self._active_goal,
            tasks=self._task_manager.to_dict_list(),
            background={
                "running": self._background.active_by_context_key(),
                "active_count": self._background.active_count(),
                "recent_events": self._execution_history[-20:],
            },
        )
        parts = []
        if turn_reminders:
            parts.append(turn_reminders)
        parts.append(context.model_dump_json(exclude_defaults=True))
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
            # result lands as a *new* message (a user-role harness note — a system
            # role here would be hoisted into Anthropic's top-level system param and
            # bust the whole prefix; see _harness_note_message). Rewriting the
            # placeholder in place would change the conversation mid-stream and
            # invalidate the provider's prompt cache from that point on — re-billing
            # the whole suffix. The placeholder already satisfies its tool_call, so
            # appending keeps the prefix monotonic (always cacheable) while the model
            # still sees the result. Same canonical envelope as an inline tool
            # result, wrapped so the model reads it as a background delivery.
            background_status, background_code = _model_result_status(
                capped_result, ok=True, backgrounded=False,
            )
            self._conversation.append(self._harness_note_message(
                _model_visible_tool_result(
                    capped_result, background_metadata, background_status, background_code,
                    kind="background_result",
                ),
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

    def _drain_spawned_agent_events(self) -> list[StreamEvent]:
        """Every live agent event queued since the last drain. Yielded by the
        turn loop so a non-blocking spawned agent's activity streams to the panel
        while the parent works, and any backlog flushes on the wake turn."""
        events: list[StreamEvent] = []
        while not self._spawned_agent_events.empty():
            events.append(self._spawned_agent_events.get_nowait())
        return events

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Fast local token estimate (~4 chars/token) for sizing the observation log,
        which has no provider usage figure of its own."""
        return len(text) // 4

    def _observation_message(self) -> "SystemMessage | None":
        """The observation-log system message (the folded conversation memory), if one
        exists. Tagged in ``additional_kwargs`` so it survives persistence + reload and
        can be found and appended to."""
        for message in self._conversation:
            if isinstance(message, SystemMessage) and message.additional_kwargs.get("observation_log"):
                return message
        return None

    async def _emit_observations(self, request: list) -> list[dict]:
        """Run one Observer/Reflector call and read its structured output from the
        model's ``ObservationBatch`` tool call — the shape is guaranteed by tool-calling,
        not scraped from free text (same pattern as BashPermissionDecision). Returns []
        when the model emits no tool call, so a miss simply changes nothing."""
        model = self._llm.bind_tools([ObservationBatch], tool_choice="auto")
        response = await model.ainvoke(request)
        if response is None or not response.tool_calls:
            return []
        try:
            batch = ObservationBatch.model_validate(response.tool_calls[0]["args"])
        except ValidationError:
            return []
        return [observation.model_dump() for observation in batch.observations]

    def _observations_of(self, message: "SystemMessage | None") -> list[dict]:
        raw = message.additional_kwargs.get("observations") if message else None
        return list(raw) if isinstance(raw, list) else []

    def _build_observation_message(self, observations: list[dict]) -> SystemMessage:
        """The observation log the main agent passively reads: the structured entries
        dumped as JSON into the render template. The list itself rides in
        ``additional_kwargs`` so a later pass appends to structured data, not to prose."""
        rendered = json.dumps(observations, ensure_ascii=False, separators=(",", ":"))
        return SystemMessage(
            content=self._prompt_loader.load("observation_log", {"observations": rendered}),
            additional_kwargs={"observation_log": True, "observations": observations},
        )

    def _observer_boundary(self) -> int:
        """Index splitting the conversation into ``[older to fold] | [recent kept
        verbatim]``. Cuts at a user-turn boundary (a HumanMessage) so tool_call/
        tool_result pairing stays intact and the kept tail is a whole number of recent
        turns. Returns 0 when nothing is old enough to fold."""
        keep = max(1, self._global_configuration.compaction.keep_recent_turns)
        human_indices = [
            index for index, message in enumerate(self._conversation)
            if isinstance(message, HumanMessage)
            # Harness notes ride in user-role messages for cache reasons but are
            # not user turns — they must not shift the keep-recent boundary.
            and not message.additional_kwargs.get("harness_note")
        ]
        if len(human_indices) <= keep:
            return 0
        return human_indices[-keep]

    def _should_compact(self) -> bool:
        """Auto-compaction trigger: enabled in config, live context past the observer
        fraction of the window, and something old enough to fold. Manual compaction
        ignores this and always runs a pass."""
        compaction = self._global_configuration.compaction
        if not compaction.auto or self._context_window <= 0:
            return False
        if self._latest_context_tokens < compaction.observer_context_fraction * self._context_window:
            return False
        return self._observer_boundary() > 0

    async def _observe(self, older: list, existing: list[dict]) -> list[dict]:
        """Fold the older messages into new structured observations to append. The
        messages are handed to the model as-is; the existing memory is shown so it does
        not duplicate what is already recorded."""
        existing_json = json.dumps(existing, ensure_ascii=False, separators=(",", ":")) if existing else "[]"
        instructions = self._prompt_loader.load("observer", {"existing_observations": existing_json})
        return await self._emit_observations([
            SystemMessage(content=instructions),
            *older,
            HumanMessage(content="Record the observations now."),
        ])

    async def _reflect(self, observations: list[dict]) -> list[dict]:
        """Merge and condense the structured memory. Keeps the original entries if
        reflection returns nothing, so it never loses memory."""
        instructions = self._prompt_loader.load(
            "reflector", {"observations": json.dumps(observations, ensure_ascii=False, separators=(",", ":"))}
        )
        reflected = await self._emit_observations([
            SystemMessage(content=instructions),
            HumanMessage(content="Record the condensed memory now."),
        ])
        return reflected or observations

    async def compact(self, reason: str = "manual") -> AsyncIterator[StreamEvent]:
        """Observational-memory compaction (replaces wholesale summarization). The
        Observer folds the older turns into the observation log — appended to what is
        already there, so the observation prefix stays cache-stable — and drops their raw
        messages; the Reflector condenses the log when it grows past its fraction of the
        window. Recent turns stay verbatim. Yields COMPACTION_STARTED/DONE for the UI. A
        no-op yields nothing. Manual calls force a pass; the auto trigger is gated by
        :meth:`_should_compact`."""
        boundary = self._observer_boundary()
        if boundary <= 0:
            return
        observation_message = self._observation_message()
        existing = self._observations_of(observation_message)
        # Fold every older message except the existing observation block (it is rebuilt).
        older = [message for message in self._conversation[:boundary] if message is not observation_message]
        recent = list(self._conversation[boundary:])
        tokens_before = self._latest_context_tokens
        messages_before = len(self._conversation)
        yield StreamEvent(
            StreamEvent.Type.COMPACTION_STARTED,
            reason=reason,
            messages_before=messages_before,
            tokens_before=tokens_before,
        )
        new_observations = await self._observe(older, existing)
        if not new_observations:
            # Produced nothing parseable — leave history untouched rather than drop it.
            yield StreamEvent(
                StreamEvent.Type.COMPACTION_DONE,
                reason=reason, ok=False,
                messages_before=messages_before,
                messages_after=messages_before,
                tokens_before=tokens_before,
            )
            return
        merged = [*existing, *new_observations]
        compaction = self._global_configuration.compaction
        merged_tokens = self._estimate_tokens(json.dumps(merged, ensure_ascii=False))
        if self._context_window > 0 and merged_tokens > compaction.reflector_observation_fraction * self._context_window:
            merged = await self._reflect(merged)
        # Replace in place: the conversation list object is shared with the executor's
        # per-context store, so mutating the same object keeps that binding.
        self._conversation[:] = [self._build_observation_message(merged), *recent]
        # Occupancy no longer reflects the (smaller) context; reset so auto-compaction
        # does not immediately re-fire before the next real model call.
        self._latest_context_tokens = 0
        yield StreamEvent(
            StreamEvent.Type.COMPACTION_DONE,
            reason=reason, ok=True,
            observations_added=len(new_observations),
            messages_before=messages_before,
            messages_after=len(self._conversation),
            tokens_before=tokens_before,
        )

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

    def _model_supports_vision(self) -> bool:
        """Whether the agent's model advertises image input. Unknown models (a
        custom endpoint not in the catalog) are assumed vision-capable — the
        same permissive default the attachment-inlining path uses."""
        model = find_model(self._effective_model_identifier)
        return True if model is None else model.vision

    def _harness_note_message(self, content: str, image_blocks: list[dict] | None = None) -> HumanMessage:
        """Wrap a harness-injected note in a user-role message carrying a
        ``<system-reminder>`` block.

        The role is deliberate — this is what keeps the conversation strictly
        append-only for the provider's prompt cache. A mid-conversation
        ``role:"system"`` message is HOISTED by LiteLLM into Anthropic's top-level
        ``system`` parameter, which renders before the entire message history; every
        such note therefore rewrites the prompt prefix and invalidates the cache for
        the whole conversation. A user-role note stays exactly where it was appended,
        so the prefix never changes — only grows — on every provider. The wrapper
        itself lives in the ``harness_note`` prompt template (wording stays in
        files, not code); it tells the model this is authoritative harness
        guidance, not user input (see the Harness Guidance section of the system
        prompt). The ``harness_note`` marker keeps these notes from counting as
        user turns in the compaction boundary. ``image_blocks`` (OpenAI-shaped
        ``image_url`` blocks) turn the note multimodal — the user role is the one
        role every provider accepts images on, which is how a read image reaches
        a vision model."""
        text = self._prompt_loader.load("harness_note", {"content": content.strip()}).strip()
        if image_blocks:
            return HumanMessage(
                content=[{"type": "text", "text": text}, *image_blocks],
                additional_kwargs={"harness_note": True},
            )
        return HumanMessage(content=text, additional_kwargs={"harness_note": True})

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

    def artifact_render_error_note(self, payload: str) -> str:
        """Frame an artifact render failure as a behind-the-scenes self-realization
        note (injected as a harness note, not user input) the model repairs. The
        raw error rides along as its JSON payload, intact."""
        return self._prompt_loader.load("artifact_render_error", {"payload": payload})

    async def stream(
        self, user_message: str | list, as_system_note: bool = False
    ) -> AsyncIterator[StreamEvent]:
        self._abort_event.clear()
        self._calls_this_turn = 0

        # A turn's input is usually plain text, but an attachment turn carries a
        # multimodal content list (a text block plus one image_url block per
        # attached image) so a vision model actually sees the pixels. LangChain's
        # HumanMessage accepts either, and the model adapter passes the content
        # straight through to the provider. A self-realization note (e.g. an artifact
        # render error) enters as a <system-reminder> harness note so the model
        # treats it as its own observation, not as something the user said — in a
        # user-role message so the append stays cache-safe (_harness_note_message).
        turn_message = (
            self._harness_note_message(user_message)
            if as_system_note and isinstance(user_message, str)
            else HumanMessage(content=user_message)
        )
        self._conversation.append(turn_message)
        # The event-log recorder only wants the prose; LangChain's own `.text`
        # accessor yields it (dropping any media blocks) — no hand-rolled flattening.
        recorded_user_message = turn_message.text

        turn_tool_calls_log: list[dict] = []
        turn_tool_results_log: list[dict] = []
        turn_final_response = ""
        goal_continuations = 0

        effective_maximum_iterations = self._agent_configuration.maximum_iterations
        if self._delegation_depth > 0:
            effective_maximum_iterations = min(
                effective_maximum_iterations, self._AGENT_MAXIMUM_ITERATIONS
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

            # Flush any live activity from non-blocking spawned agents so the panel
            # tracks them while the parent works through its own iterations.
            for agent_event in self._drain_spawned_agent_events():
                yield agent_event

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

            # Auto-compaction: if the last call left the context near the window,
            # summarize the older history before making another call that could
            # overflow. The reserved buffer guarantees room to run the compaction
            # itself; compact() resets the occupancy so this cannot re-fire in a loop.
            if self._should_compact():
                async for compaction_event in self.compact(reason="auto"):
                    yield compaction_event

            # Dynamic context (turn reminders, time, pwd, active goal) is injected
            # only on the first iteration of a turn — when the user just sent a
            # message. Subsequent iterations (after tool calls) skip it to avoid
            # re-sending the same reminders on every LLM call within the turn.
            # Delivered as a transient user-role harness note at the very tail of
            # the request — never as a system message (LiteLLM would hoist it into
            # Anthropic's top-level system param, whose fresh timestamp would then
            # invalidate the ENTIRE conversation cache on every turn). As a tail
            # note, everything before it still prefix-matches the provider cache.
            dynamic_parts = (
                [self._harness_note_message(self._build_dynamic_context())]
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
            # Drive the model stream one read at a time, racing each read against the
            # abort event, so a Stop interrupts the turn *immediately* — even while it
            # is parked awaiting the next token from a slow or stalled provider. Only
            # checking the flag between chunks (the previous behaviour) let a provider
            # that had gone quiet swallow the cancel until it happened to emit again,
            # which is why Stop "sometimes" appeared to do nothing.
            model_stream = self._bound_llm.astream(messages)
            abort_waiter = asyncio.ensure_future(self._abort_event.wait())
            try:
                while True:
                    chunk_future = asyncio.ensure_future(_stream_next(model_stream))
                    await asyncio.wait(
                        {chunk_future, abort_waiter}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if self._abort_event.is_set():
                        # Stop won the race (or landed between chunks): drop the pending
                        # read and stop consuming the stream (the `finally` closes it).
                        chunk_future.cancel()
                        with suppress(BaseException):
                            await chunk_future
                        if self._has_queued_steering():
                            self._abort_event.clear()
                            aborted_for_steering = True
                            break
                        yield StreamEvent(StreamEvent.Type.DONE, text="", stop_reason="cancelled")
                        return
                    chunk = chunk_future.result()
                    if chunk is _STREAM_EXHAUSTED:
                        break
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
            finally:
                abort_waiter.cancel()
                # Close the underlying HTTP stream so an aborted (or exhausted) turn
                # never leaks a provider connection.
                with suppress(BaseException):
                    stream_closer = getattr(model_stream, "aclose", None)
                    if stream_closer is not None:
                        await stream_closer()
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
                    # Correct the model with a harness note and let it retry. This is
                    # model-facing, so it is not surfaced to the user.
                    if response.content:
                        self._conversation.append(response)
                    for invalid in response.invalid_tool_calls:
                        self._conversation.append(self._harness_note_message(
                            self._invalid_tool_call_content(cast(dict, invalid)),
                        ))
                    self._calls_this_turn += 1
                    continue

                # The model produced no tool calls. Any still-running background work
                # does not hold the turn open: it ends here, and the executor's resume
                # pump wakes the agent with an autonomous turn once the next result
                # lands. Results that already completed were drained at the top of the
                # loop, so nothing in hand is lost by finishing now. `.text` is the
                # library-native prose accessor — always a string, whether the model
                # returned plain text or a content-block list.
                final_text = response.text
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
                    self._conversation.append(self._harness_note_message(goal_continuation))
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
                tool_calls = cast(list[dict], response.tool_calls)
                async for event in self._drain_tools_concurrently(
                    tool_calls, turn_tool_calls_log, turn_tool_results_log, outcomes,
                ):
                    yield event

            # Append the initiating AIMessage, then a ToolMessage for every call.
            # The ToolMessage block must stay CONTIGUOUS: providers require every
            # tool_call's result in the immediately-following turn (LiteLLM merges
            # consecutive tool messages into one Anthropic user turn), so any
            # harness notes (denied commands) are appended after the whole block.
            self._conversation.append(response)
            denied_command_notes: list[str] = []
            image_followup_notes: list[dict[str, str]] = []
            for tool_call_data in cast(list[dict], response.tool_calls):
                tool_call_identifier = tool_call_data["id"]
                outcome = outcomes.get(tool_call_identifier, {})
                content = outcome.get("content", "")
                if not content:
                    content = "(interrupted)" if self._abort_event.is_set() else ""
                result_status, result_code = _model_result_status(
                    content,
                    ok=outcome.get("ok", True),
                    backgrounded=bool(outcome.get("background_task_identifier")),
                )
                model_visible_content = _model_visible_tool_result(
                    content,
                    outcome.get("metadata") or _tool_timing_metadata(
                        tool_name=tool_call_data.get("name", ""),
                        tool_call_identifier=tool_call_identifier,
                        started_at=datetime.now(timezone.utc),
                        completed_at=datetime.now(timezone.utc),
                        duration_milliseconds=0,
                    ),
                    result_status,
                    result_code,
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
                    denied_command_notes.append(
                        self._prompt_loader.load("command_denied", {"commands": commands_list})
                    )
                image_followup_notes.extend(outcome.get("image_followups") or [])
            # Images read this round attach right after the tool block, as
            # image-bearing harness notes — the append-only, every-provider way
            # for a vision model to see pixels a tool produced.
            for followup in image_followup_notes:
                note_text = self._prompt_loader.load("image_read_note", {"path": followup.get("path", "")})
                self._conversation.append(self._harness_note_message(
                    note_text,
                    image_blocks=[{"type": "image_url", "image_url": {"url": followup["data_uri"]}}],
                ))
            for denied_message in denied_command_notes:
                self._conversation.append(self._harness_note_message(denied_message))

            # Malformed tool calls serialized alongside valid ones: correct them
            # with a harness note (not a ToolMessage — invalid calls aren't in the
            # serialized tool_calls, so a ToolMessage would be orphaned and rejected
            # by strict providers). Model-facing; not surfaced to the user.
            for invalid in response.invalid_tool_calls:
                self._conversation.append(self._harness_note_message(
                    self._invalid_tool_call_content(cast(dict, invalid)),
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

    def _load_agent(self, name: str) -> AgentConfiguration:
        return load_agent_configuration(
            name,
            self._global_configuration.agent_directories_for(self._project_directory),
        )

    def _build_agent_prompt(self, prompt: str, read_only: bool | None) -> str:
        mode = "read-only investigation" if read_only else "delegated task"
        return self._prompt_loader.load("agent_task", {
            "mode": mode,
            "prompt": prompt,
        })

    def _relay_child_event(self, delegated: dict, group_id: str, step_id: str) -> "StreamEvent | None":
        """Wrap one relayed agent event for re-emission. The child already speaks
        the unified vocabulary; we only prefix this agent's path segment so it renders
        at the right depth. Non-event control signals (started/done) carry no panel
        update and return ``None``."""
        if delegated.get("type") != "event":
            return None
        child_event = dict(delegated["event"])
        segment = {"group_id": group_id, "step_id": step_id}
        child_event["path"] = [segment, *child_event.get("path", [])]
        return StreamEvent(StreamEvent.Type.RELAYED, event=child_event)

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
        image_followups: list[dict[str, str]] = []
        tool_failed = False

        try:
            async for event in self._execute_tool(tool_name, tool_arguments, tool_call_identifier):
                # An image read carries its pixels on a model-facing side channel;
                # strip it here so the base64 never reaches the UI or the event
                # log — the turn loop attaches it to the conversation instead.
                if event.type == StreamEvent.Type.TOOL_RESULT and "model_image" in event.data:
                    result_payload = event.data.get("result")
                    image_followups.append({
                        "path": str((result_payload or {}).get("path", "")) if isinstance(result_payload, dict) else "",
                        "data_uri": str(event.data.pop("model_image")),
                    })
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
                            # A structured result that reports an error status marks the
                            # call failed for the model and the UI alike.
                            if tool_status_from_result(result_str) == ToolStatus.ERROR:
                                tool_failed = True
                            # Minified for the model — no spaces, and non-ASCII kept verbatim
                            # (window titles, emoji) rather than \uXXXX-escaped. This is the one
                            # place every structured tool result (e.g. the computer tool's element
                            # list) is serialized for the model; the UI receives the dict on a
                            # separate path and prettifies it there, so this never affects display.
                            result_str = json.dumps(result_str, ensure_ascii=False, separators=(",", ":"))
                        result_content = _cap_model_result_payload(str(result_str))
                        turn_tool_results_log.append({"name": tool_name, "result": result_content})
                elif event.type == StreamEvent.Type.ERROR:
                    tool_failed = True
                    result_content = event.data.get("message", "unknown error")
                    turn_tool_results_log.append({"name": tool_name, "result": result_content})
                elif event.type == StreamEvent.Type.DENIED_INJECTION:
                    denied_commands.append(event.data.get("command", ""))
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
            "ok": not tool_failed,
            "background_task_identifier": background_task_identifier,
            "denied_commands": denied_commands,
            "image_followups": image_followups,
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
        arrive (interleaved). The model emits several tool calls in one response
        when work is parallel; they run concurrently so multiple spawned agents
        and other independent tools progress together."""
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
        abort_waiter = asyncio.ensure_future(self._abort_event.wait())
        try:
            while True:
                if self._abort_event.is_set():
                    break
                # Race the next tool event against the abort so a Stop is honored even
                # when every tool is parked (a slow network/MCP call, a thread that
                # can't be cancelled) and nothing is arriving on the queue. The
                # `finally` cancels the runners; the turn loop then records "(interrupted)"
                # for any tool that never produced a result.
                get_future = asyncio.ensure_future(queue.get())
                await asyncio.wait(
                    {get_future, abort_waiter}, return_when=asyncio.FIRST_COMPLETED
                )
                if not get_future.done():
                    get_future.cancel()
                    break
                event = get_future.result()
                if event is None:
                    break
                yield event
        finally:
            abort_waiter.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _validate_tool_call(
        self,
        tool_name: str,
        arguments: dict,
    ) -> tuple[str, str] | None:
        tool_schemas = self._tool_schemas
        if not isinstance(arguments, dict):
            return ("invalid_tool_arguments", f"{tool_name} arguments must be an object.")
        if tool_name not in tool_schemas:
            accepted = ", ".join(sorted(tool_schemas))
            return ("unknown_tool", f"Unknown or unavailable tool '{tool_name}'. Available tools are: {accepted}.")
        schema = tool_schemas.get(tool_name)
        if schema is not None:
            fields = set(getattr(schema, "model_fields", {}).keys())
            unknown_arguments = sorted(set(arguments) - fields)
            if unknown_arguments:
                accepted = ", ".join(sorted(fields))
                rejected = ", ".join(unknown_arguments)
                return ("invalid_tool_arguments", f"The tool {tool_name} does not accept the following argument(s) with which it was invoked: {rejected}. Accepted arguments by it are only: {accepted}.")
            # `call_mcp_tool.arguments` is frequently emitted by models as a JSON *string*
            # rather than an object. Coerce it to a dict for validation (dispatch coerces it
            # the same way via _coerce_mcp_arguments), so a stringified-but-valid arguments
            # object is accepted instead of being rejected by the dict-typed schema field.
            if tool_name == "call_mcp_tool" and isinstance(arguments.get("arguments"), str):
                arguments = {**arguments, "arguments": _coerce_mcp_arguments(arguments.get("arguments"))}
            try:
                schema_validator = getattr(schema, "model_validate", None)
                if schema_validator is not None:
                    schema_validator(arguments)
            except ValidationError as exception:
                return ("invalid_tool_arguments", str(exception))
        if tool_name in ("bash", "call_mcp_tool"):
            risk = arguments.get("risk", "low")
            if risk not in ("low", "medium", "high"):
                return ("invalid_risk", f"risk must be one of 'low', 'medium', 'high', got '{risk}'.")
            # Omitted read_only is treated as mutating — the conservative default.
            read_only = arguments.get("read_only", False)
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

    def _outside_working_directory_reads(self, command: str, working_directory: str | None = None) -> list[str]:
        """Best-effort static path boundary check for bash commands.

        The shell remains too dynamic to prove every access, so this intentionally
        catches explicit path arguments that leave the session working directory:
        absolute paths, home paths, and parent-directory traversal.
        """
        if not self._global_configuration.sandbox.enabled:
            return []
        root = Path(working_directory or self._working_directory or Path.home()).expanduser()
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
        TOOL_RESULT, ERROR, and BACKGROUND_STARTED events."""

        # Coerce any list/dict argument the model passed as a JSON string into its native
        # value up front, so validation and dispatch both see the real container.
        schema = self._tool_schemas.get(tool_name)
        if schema is not None:
            tool_arguments = _coerce_structured_arguments(schema, tool_arguments)

        try:
            self._permissions.check_tool(tool_name, **tool_arguments)
        except PermissionError as exception:
            yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message=str(exception), tool=tool_name)
            return

        validation_error = self._validate_tool_call(
            tool_name,
            tool_arguments,
        )
        if validation_error:
            error_code, error_message = validation_error
            yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, code=error_code, message=error_message, tool=tool_name)
            return

        # Filesystem/shell tools run against a specific project location. Resolve it
        # here and derive this call's execution policy as a value — the location's
        # executor carries the IO (local subprocess or multiplexed SSH) through one
        # shared code path. Nothing location-specific is written to runtime state, so
        # tool calls running concurrently against different locations cannot cross
        # policies or working directories.
        resolved_location: ResolvedLocation | None = None
        if tool_name in _LOCATION_TOOLS:
            tool_arguments = dict(tool_arguments)
            location_value = tool_arguments.pop("location", None) or None
            try:
                resolved_location = self._resolve_location(location_value)
            except ToolLocationError as exception:
                yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, code="invalid_location", message=str(exception), tool=tool_name)
                return
        policy = self._call_policy(resolved_location)

        if tool_name == "bash":
            raw_command = tool_arguments.get("command", "")
            if policy.is_remote:
                # A remote command runs as a local `ssh …` invocation over the
                # location's multiplexed connection, so the ordinary bash machinery
                # below — sync ceiling, backgrounding, output capping + overflow
                # file, cancellation — drives it unchanged. All permission analysis
                # (static read-only classification, allow rules, prompts) runs on
                # the raw remote command, never on the ssh wrapper.
                from harness.locations.executor import SshExecutor

                assert resolved_location is not None
                executor = resolved_location.executor
                # A remote policy always resolves to the ssh-backed executor.
                assert isinstance(executor, SshExecutor)
                tool_arguments = dict(tool_arguments)
                tool_arguments["command"] = shlex.join(
                    executor.ssh_argv(raw_command, resolved_location.base_directory)
                )
            else:
                directory = policy.working_directory
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
            read_only = tool_arguments.get("read_only", False)
            if isinstance(read_only, str):
                read_only = read_only.lower() == "true"

            # A command the user chose to "always allow" this session skips both
            # the sandbox and approval prompts below.
            session_allowed = self._command_session_allowed(raw_command)

            static_classification, static_detail = self._agent_configuration.tools.bash.read_only_assessment(raw_command)
            # Path-boundary sandboxing protects THIS machine's filesystem; a remote
            # command only touches remote paths, so the check does not apply there.
            outside_reads = (
                []
                if policy.is_remote
                else self._outside_working_directory_reads(raw_command, policy.working_directory)
            )
            # Bypass mode means bypass: no sandbox or risk-based approval prompts.
            # (The hard blocks below — read-only enforcement and explicit deny
            # rules — still apply; bypass only skips the human-in-the-loop asks.)
            if outside_reads and not session_allowed and not policy.bypass_permissions:
                paths = ", ".join(outside_reads)
                sandbox_message = (
                    f"Sandbox approval required: this command reads outside the working directory ({paths})."
                )
                if self._is_agent:
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
                        command=raw_command,
                    )
                    return
                permission_decision = self._evaluate_bash_permission(raw_command, bypass=policy.bypass_permissions)
                if policy.auto_permissions and permission_decision != "deny":
                    decision = await self._classify_bash_permission(
                        command=raw_command,
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
                            command=raw_command,
                            justification=decision.justification or sandbox_message,
                            risk=decision.risk,
                        )
                        allowed = await self._resolve_permission_future(request_identifier, future, raw_command, is_bash=True)
                        if not allowed:
                            yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message="Sandbox read was not approved by the user.", tool=tool_name)
                            return
                else:
                    if permission_decision == "deny":
                        yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message="Sandbox read denied by the default permission policy.", tool=tool_name)
                        return
                    request_identifier = f"perm-{self._session_id}-{uuid.uuid4()}"
                    future = asyncio.get_event_loop().create_future()
                    self._pending_permissions[request_identifier] = future
                    yield StreamEvent(
                        StreamEvent.Type.PERMISSION_REQUEST,
                        id=tool_call_identifier,
                        request_id=request_identifier,
                        command=raw_command,
                        justification=sandbox_message,
                        risk="medium",
                    )
                    allowed = await self._resolve_permission_future(request_identifier, future, raw_command, is_bash=True)
                    if not allowed:
                        yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message="Sandbox read was not approved by the user.", tool=tool_name)
                        return

            # Read-only agents and read-only locations may only run read-only
            # commands. Static analysis classifies the command (local and remote
            # alike — never the model's own declaration alone); a detected mutation
            # is always blocked, and a command that can't be classified is blocked
            # only when the model itself marked it as a write. This is a hard
            # block — agents have no human in the loop to approve.
            if policy.read_only:
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
                        command=raw_command,
                    )
                    return

            permission_decision = self._evaluate_bash_permission(raw_command, bypass=policy.bypass_permissions)
            if permission_decision == "deny":
                deny_message = f"Command '{raw_command}' is not permitted."
                yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message=deny_message, tool=tool_name)
                yield StreamEvent(
                    StreamEvent.Type.DENIED_INJECTION,
                    id=tool_call_identifier,
                    command=raw_command,
                )
                return
            elif (
                not session_allowed
                and not policy.bypass_permissions
                and (permission_decision == "ask" or risk in ("medium", "high"))
            ):
                if policy.auto_permissions:
                    decision = await self._classify_bash_permission(
                        command=raw_command,
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
                            command=raw_command,
                            justification=decision.justification or justification,
                            risk=decision.risk,
                        )
                        allowed = await self._resolve_permission_future(request_identifier, future, raw_command, is_bash=True)
                        if not allowed:
                            yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message="Command was not approved by the user.", tool=tool_name)
                            return
                else:
                    request_identifier = f"perm-{self._session_id}-{uuid.uuid4()}"
                    future = asyncio.get_event_loop().create_future()
                    self._pending_permissions[request_identifier] = future
                    yield StreamEvent(
                        StreamEvent.Type.PERMISSION_REQUEST,
                        id=tool_call_identifier,
                        request_id=request_identifier,
                        command=raw_command,
                        justification=justification,
                        risk=risk,
                    )
                    allowed = await self._resolve_permission_future(request_identifier, future, raw_command, is_bash=True)
                    if not allowed:
                        yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message="Command was not approved by the user.", tool=tool_name)
                        return

            lease_token = ""
            # Filesystem leases guard this machine's working trees; a remote command
            # mutates the remote host, so no local lease applies.
            if not read_only and not policy.is_remote:
                try:
                    lease_token = await self._acquire_filesystem_lease(
                        scope="worktree",
                        path=self._canonical_working_directory(policy.working_directory),
                        description=f"mutating bash: {raw_command[:160]}",
                        working_directory=policy.working_directory,
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
                tool_call_token = bind_tool_call_id(tool_call_identifier)
                try:
                    result = await bash_tool.ainvoke(tool_arguments)
                finally:
                    unbind_tool_call_id(tool_call_token)
                    unbind_background_jobs(background_token)
                result_data = _maybe_json(result)
                yield StreamEvent(StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=result_data)
                is_background_command = isinstance(result_data, dict) and result_data.get("code") == "bash_started"
                if resolved_location is not None and not read_only and not is_background_command:
                    # A completed mutating command may have regenerated a file we already
                    # track — restage only the tracked set (cheap; never surveys the folder).
                    self._capture_written_artifacts(
                        resolved_location, changed_absolute_paths=None, mode="recheck",
                        tool_call_id=tool_call_identifier, message=f"bash: {raw_command[:80]}",
                    )
                if isinstance(result_data, dict) and result_data.get("code") == "bash_started":
                    task_identifier = result_data.get("task_identifier", "")
                    if task_identifier:
                        self._record_event("background_bash_started", {"task_identifier": task_identifier, "command": raw_command})
                        if lease_token and self._background.add_done_callback(
                            task_identifier,
                            lambda _identifier, token=lease_token: self._release_filesystem_lease(token),
                        ):
                            lease_token = ""
            finally:
                self._release_filesystem_lease(lease_token)

        elif tool_name == "read_file":
            assert resolved_location is not None
            file_path = str(tool_arguments.get("file_path", ""))
            # Image files are ingested natively: the tool result carries structured
            # metadata (mime, dimensions, size), and when the model has vision the
            # pixels ride along as a data URI on the event under `model_image` —
            # a model-facing side channel _run_one_tool strips before the event
            # reaches the UI, then attaches to the conversation after the tool
            # block as an image-bearing harness note.
            if Path(file_path).suffix.lower() in file_tools.IMAGE_FILE_SUFFIXES:
                result, image_data_uri = await asyncio.to_thread(
                    file_tools.read_image_file,
                    resolved_location.executor,
                    resolved_location.base_directory,
                    file_path,
                    attach_pixels=self._model_supports_vision(),
                )
                extra = {"model_image": image_data_uri} if image_data_uri else {}
                yield StreamEvent(
                    StreamEvent.Type.TOOL_RESULT,
                    id=tool_call_identifier, name=tool_name, result=_maybe_json(result), **extra,
                )
                return
            offset = tool_arguments.get("offset", 1) or 1
            limit_raw = tool_arguments.get("limit")
            limit = int(limit_raw) if limit_raw not in (None, "") else None
            result = await asyncio.to_thread(
                file_tools.read_file,
                resolved_location.executor,
                resolved_location.base_directory,
                file_path,
                int(offset),
                limit,
            )
            result_data = _maybe_json(result)
            # Record the resolved path and hash — keyed by location so the same
            # path on two hosts never collides — so edit_file/write_file can
            # reject stale edits.
            if isinstance(result_data, dict):
                sha256 = result_data.get("sha256")
                resolved_path = result_data.get("path")
                if isinstance(sha256, str) and isinstance(resolved_path, str):
                    self._read_files[(resolved_location.uri, resolved_path)] = sha256
            yield StreamEvent(
                StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=result_data,
            )

        elif tool_name == "find_files":
            assert resolved_location is not None
            pattern = str(tool_arguments.get("pattern", ""))
            result = await asyncio.to_thread(
                file_tools.find_files, resolved_location.executor, resolved_location.base_directory, pattern,
            )
            yield StreamEvent(
                StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=_maybe_json(result),
            )

        elif tool_name == "search_content":
            assert resolved_location is not None
            pattern = str(tool_arguments.get("pattern", ""))
            include = tool_arguments.get("include")
            include = str(include) if include else None
            search_path = tool_arguments.get("path")
            search_path = str(search_path) if search_path else None
            result = await asyncio.to_thread(
                file_tools.search_content,
                resolved_location.executor,
                resolved_location.base_directory,
                pattern,
                include,
                search_path,
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

        elif tool_name == "download_file":
            assert resolved_location is not None
            if policy.read_only:
                deny_message = self._prompt_loader.load("read_only_denied", {"violation": "a file download"})
                yield StreamEvent(
                    StreamEvent.Type.ERROR, id=tool_call_identifier, message=deny_message, tool=tool_name,
                )
                return
            executor = resolved_location.executor
            url = str(tool_arguments.get("url", ""))
            destination = str(tool_arguments.get("path", ""))
            timeout = int(tool_arguments.get("timeout", 120) or 120)
            resolved = await asyncio.to_thread(executor.resolve, resolved_location.base_directory, destination)
            result = await file_tools.download_file(executor, url, resolved, timeout)
            yield StreamEvent(
                StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=_maybe_json(result),
            )

        elif tool_name in ("edit_file", "write_file"):
            assert resolved_location is not None
            if policy.read_only:
                deny_message = self._prompt_loader.load("read_only_denied", {"violation": "a file modification"})
                yield StreamEvent(
                    StreamEvent.Type.ERROR, id=tool_call_identifier, message=deny_message, tool=tool_name,
                )
                return
            executor = resolved_location.executor
            file_path = str(tool_arguments.get("file_path", ""))
            resolved = await asyncio.to_thread(executor.resolve, resolved_location.base_directory, file_path)
            file_key = (resolved_location.uri, resolved)
            lease_token = ""
            # Filesystem leases guard this machine's files; a remote edit mutates
            # the remote host, so no local lease applies there.
            if not policy.is_remote:
                try:
                    lease_token = await self._acquire_filesystem_lease(
                        scope="file",
                        path=resolved,
                        description=f"{tool_name}: {resolved}",
                        working_directory=policy.working_directory,
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
                expected_sha256 = self._read_files.get(file_key)
                if tool_name == "edit_file":
                    find = str(tool_arguments.get("find", ""))
                    replace_with = str(tool_arguments.get("replace_with", ""))
                    replace_all = bool(tool_arguments.get("replace_all", False))
                    result = await asyncio.to_thread(
                        file_tools.edit_file,
                        executor,
                        resolved_location.base_directory,
                        file_path,
                        find,
                        replace_with,
                        expected_sha256=expected_sha256,
                        replace_all=replace_all,
                    )
                else:
                    content = tool_arguments.get("content", "")
                    if not isinstance(content, str):
                        content = json.dumps(content)
                    result = await asyncio.to_thread(
                        file_tools.write_file,
                        executor,
                        resolved_location.base_directory,
                        file_path,
                        content,
                        expected_sha256=expected_sha256,
                    )
                result_data = _maybe_json(result)
                if isinstance(result_data, dict):
                    result_code = result_data.get("code", "")
                    if result_code == "edit_completed":
                        sha256 = result_data.get("sha256")
                        if isinstance(sha256, str):
                            self._read_files[file_key] = sha256
                        else:
                            self._read_files.pop(file_key, None)
                    elif result_code == "write_completed":
                        content = tool_arguments.get("content", "")
                        if isinstance(content, str):
                            self._read_files[file_key] = file_tools.content_sha256(content)
                    else:
                        # edit_failed_validation or other non-commit codes:
                        # discard stale hash so model must re-read before next edit
                        self._read_files.pop(file_key, None)
                    if result_code in ("edit_completed", "write_completed"):
                        # Version exactly this file. For an edit of a pre-existing file, pass
                        # its pre-edit bytes (the tool's "before") so the first version we
                        # keep is the original — the file on disk is already the edited copy.
                        original_contents = None
                        before_content = result_data.get("before")
                        if not result_data.get("created") and isinstance(before_content, str):
                            original_contents = {resolved: before_content}
                        self._capture_written_artifacts(
                            resolved_location, changed_absolute_paths=[resolved],
                            original_contents=original_contents,
                            tool_call_id=tool_call_identifier, message=f"{tool_name} {Path(resolved).name}",
                        )
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
            match = next((skill for skill in all_skills if skill.identifier == skill_name), None)
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
            # The user dismissed the whole prompt without answering (the decline
            # sentinel from resolve_question). Report it to the model and end the
            # turn cleanly — do not proceed on a guess. Setting the abort event lets
            # the tool finish recording its result first, then the stream loop stops
            # the turn; background work the user chose to keep running is untouched.
            if isinstance(answers, dict) and answers.get("__declined__"):
                result = json.dumps({
                    "code": "user_declined",
                    "message": (
                        "The user dismissed the question without answering and chose to stop here. "
                        "Do not re-ask or proceed on a guess; wait for further direction."
                    ),
                })
                yield StreamEvent(
                    StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=_maybe_json(result),
                )
                self._abort_event.set()
                return
            result = json.dumps({"code": "user_answered", "answers": answers})
            yield StreamEvent(
                StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=_maybe_json(result),
            )

        elif tool_name == "call_mcp_tool":
            read_only = tool_arguments.get("read_only", False)
            risk = tool_arguments.get("risk", "low")
            if policy.read_only and not read_only:
                deny_message = self._prompt_loader.load("read_only_denied", {"violation": "a mutating MCP tool call"})
                yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message=deny_message, tool=tool_name)
                return
            if not policy.bypass_permissions and not read_only and risk in ("medium", "high"):
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
                    # Once the MCP call has finished, flush any events still buffered
                    # on the queue and stop. Draining synchronously with get_nowait()
                    # — rather than racing a fresh getter against the already-done
                    # call_task — is what avoids a busy-spin: asyncio.wait returns
                    # instantly on the completed call_task, so a queued getter would be
                    # cancelled before it could drain the item, leaving the queue
                    # non-empty and the loop re-arming a getter forever, pinning a core.
                    if call_task.done():
                        while not event_queue.empty():
                            yield StreamEvent(
                                StreamEvent.Type.MCP_EVENT,
                                id=tool_call_identifier,
                                name="call_mcp_tool",
                                server=tool_arguments.get("server", ""),
                                tool=tool_arguments.get("tool_name", ""),
                                event=event_queue.get_nowait(),
                            )
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
                    else:
                        # get_task is still pending (call_task completed first); cancel
                        # only the getter — never call_task, which the loop re-checks.
                        get_task.cancel()
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

            raw_agent_prompt = tool_arguments.get("prompt", "")
            # The agents panel heading is the model's user-facing justification (a
            # concise "why this agent" clause), never the whole prompt.
            agent_title = str(tool_arguments.get("justification", "")).strip()
            agent_name = tool_arguments.get("agent", self._global_configuration.default_agent)
            agent_read_only = tool_arguments.get("read_only", None)
            if isinstance(agent_read_only, str):
                agent_read_only = agent_read_only.lower() == "true"
            agent_prompt = self._build_agent_prompt(raw_agent_prompt, agent_read_only)
            spawn_step_id = new_id("agent")

            try:
                sub_configuration = self._load_agent(agent_name)
            except FileNotFoundError as exception:
                yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, message=str(exception), tool=tool_name)
                return

            # Spawning is non-blocking: the agent runs as a background job (a
            # related A2A task) and the parent continues immediately. The agent's
            # activity streams live into the agents panel via the shared event queue,
            # and its structured deliverable (the child task) is injected into the
            # conversation and wakes the parent when it finishes — the same
            # inject-and-wake path as a background bash command. So the parent never
            # blocks on an agent: it can spawn several in parallel, keep working,
            # and pick up each result as it lands.
            group_id = f"agents-{self._a2a_task_id or self._session_id}"
            yield StreamEvent(
                StreamEvent.Type.GROUP_STARTED,
                group_id=group_id,
                step_id=spawn_step_id,
                agent_name=agent_name,
                title=agent_title,
                tool_call_id=tool_call_identifier,
            )
            self._record_event("agent_spawned", {"task_identifier": spawn_step_id, "agent": agent_name, "prompt": raw_agent_prompt})
            delegate = self._delegate
            if delegate is not None:
                async def _run_spawned_agent() -> str:
                    child_task = None
                    child_task_id = ""
                    try:
                        async for delegated in delegate(
                            agent_name,
                            agent_prompt,
                            self._a2a_task_id,
                            agent_read_only,
                            child_depth,
                            self._working_directory,
                            self._project_directory,
                        ):
                            # The agent's streamed progress goes to the panel only,
                            # never into the parent's model context.
                            if delegated.get("type") == "started":
                                # Track the child's own A2A task so a Stop can reap it.
                                child_task_id = delegated.get("child_task_id", "")
                                if child_task_id:
                                    self._active_delegations[child_task_id] = agent_name
                                continue
                            if delegated.get("type") == "usage":
                                usage = delegated["event"]
                                self._add_agent_usage(
                                    int(usage.get("input_tokens", 0) or 0),
                                    int(usage.get("output_tokens", 0) or 0),
                                )
                                continue
                            event = self._relay_child_event(delegated, group_id, spawn_step_id)
                            if event is not None:
                                await self._spawned_agent_events.put(event)
                            if delegated.get("type") == "done":
                                child_task = delegated.get("task")
                                # The child's turn is a control signal, not a relayed panel
                                # event; synthesize a `done` at this step's path so the panel
                                # marks it terminal.
                                child_state = str(((child_task or {}).get("status") or {}).get("state") or "completed")
                                await self._spawned_agent_events.put(StreamEvent(
                                    StreamEvent.Type.RELAYED,
                                    event={
                                        "kind": "done",
                                        "path": [{"group_id": group_id, "step_id": spawn_step_id}],
                                        "state": child_state,
                                    },
                                ))
                        # Inject ONLY the agent's final report (its deliverable
                        # artifact) into the parent — not the whole task object, and not
                        # any of the intermediate progress. The parent asked for a result,
                        # not a transcript.
                        return _spawned_agent_report(child_task, agent_name)
                    finally:
                        self._active_delegations.pop(child_task_id, None)

                # Non-detached: a Stop cancels the agent (it is active task work),
                # but merely ending the turn does not — the job outlives the turn and
                # wakes the parent when it completes.
                spawned_task_id = self._background.spawn(
                    "spawn_agent",
                    _run_spawned_agent(),
                    tool_call_identifier=tool_call_identifier,
                    arguments={"agent": agent_name, "prompt": raw_agent_prompt, "read_only": agent_read_only},
                )
                yield StreamEvent(
                    StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name,
                    result={
                        "code": "agent_started",
                        "task_identifier": spawned_task_id,
                        "agent": agent_name,
                        "message": self._prompt_loader.load("agent_started_note", {"agent": agent_name}).strip(),
                    },
                )
            else:
                child_task = None
                runner = AgentRunner(
                    agent_configuration=sub_configuration,
                    global_configuration=self._global_configuration,
                    task_identifier=spawn_step_id,
                    prompt=agent_prompt,
                    stream_progress=self._agent_configuration.stream_agent_progress,
                    read_only_override=agent_read_only,
                    working_directory=self._working_directory,
                    project_directory=self._project_directory,
                )

                # Run the agent as a background task so the parent continues
                # immediately. The agent runs to completion silently; its
                # result is stored and can be collected later via the agents panel
                # or a subsequent read_task call. No intermediate events are
                # forwarded — that would bloat the parent's context.
                async def _run_background() -> None:
                    final_text = ""
                    async for event in runner.run_stream(always_yield_text=True):
                        if event.type == StreamEvent.Type.DONE:
                            final_text = event.data.get("text", final_text)
                    done_task = serialize_task(build_task(spawn_step_id, agent_name, TaskState.completed, final_text))
                    # Store the result so it can be retrieved later.
                    self._background_agent_results[spawn_step_id] = done_task

                self._background_agent_results.setdefault(spawn_step_id, None)
                asyncio.create_task(_run_background())
                # Return immediately — parent continues its turn.
                child_task = {"code": "running", "message": f"Agent {agent_name} is running in the background.", "task_id": spawn_step_id}
                yield StreamEvent(
                    StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name,
                    result=child_task or {"code": "empty_response", "message": "Agent produced no task."},
                )

        elif tool_name == "set_tasks":
            task_definitions = tool_arguments.get("tasks", [])
            identifiers = self._task_manager.add_tasks(task_definitions)
            result_message = f"Created {len(identifiers)} task{'s' if len(identifiers) != 1 else ''}."
            # A normal tool_result — the task list is the model's own bookkeeping, so it
            # completes through the one universal completion path like any other tool. The
            # full task snapshot goes to both the model (it should see the authoritative
            # plan inline) and the UI task panel.
            yield StreamEvent(
                StreamEvent.Type.TOOL_RESULT,
                id=tool_call_identifier,
                name=tool_name,
                result={
                    "code": "tasks_updated",
                    "message": result_message,
                    "tasks": self._task_manager.to_dict_list(),
                },
            )

        elif tool_name == "update_tasks":
            updates = tool_arguments.get("updates", [])
            updated_ids = self._task_manager.update_tasks(updates)
            if updated_ids:
                result_message = f"Updated {len(updated_ids)} task{'s' if len(updated_ids) != 1 else ''}."
            else:
                result_message = "No matching tasks found."
            yield StreamEvent(
                StreamEvent.Type.TOOL_RESULT,
                id=tool_call_identifier,
                name=tool_name,
                result={
                    "code": "tasks_updated",
                    "message": result_message,
                    "tasks": self._task_manager.to_dict_list(),
                },
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
                    "message": "Status must be one of 'active', 'satisfied', or 'cleared'.",
                }
            yield StreamEvent(StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=result)

        elif tool_name == "open_artifact":
            if self._is_agent:
                yield StreamEvent(
                    StreamEvent.Type.ERROR,
                    id=tool_call_identifier,
                    tool=tool_name,
                    code="agent_artifact_denied",
                    message="Agents cannot open artifacts. Return findings only as text for the parent agent.",
                )
                return
            raw_target = str(tool_arguments.get("url", "")).strip()
            if not raw_target:
                yield StreamEvent(
                    StreamEvent.Type.ERROR, id=tool_call_identifier, tool=tool_name,
                    code="empty_artifact", message="A URL or file path is required to open an artifact.",
                )
                return
            requested_artifact_id = str(tool_arguments.get("artifact_id", "")).strip()
            requested_height = tool_arguments.get("height", 0)
            lowered = raw_target.lower()

            if lowered.startswith(("http://", "https://")):
                # An external URL renders as a live iframe with no version history — only
                # surface it (no capture). A stable id derived from the URL reuses one tab.
                artifact_id = requested_artifact_id or self._artifact_surface_id(raw_target)
                self._capture_written_artifacts(
                    self._resolve_location(None), changed_absolute_paths=[],
                    tool_call_id=tool_call_identifier, message="open_artifact",
                    surface={"surface_id": artifact_id, "kind": "iframe", "title": raw_target, "source": raw_target, "absolute_path": ""},
                )
                result = build_open_artifact_result(
                    artifact_id=artifact_id, kind="iframe", title=raw_target, source=raw_target,
                    url=raw_target, height=requested_height,
                )
                yield StreamEvent(StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=result)
                return

            # A local (or remote) file path, resolved against a location. Capture the file
            # as a version and surface it as a tab; its history is served from the shadow repo.
            location_value = tool_arguments.get("location", None) or None
            try:
                resolved_location = self._resolve_location(location_value)
            except ToolLocationError as exception:
                yield StreamEvent(StreamEvent.Type.ERROR, id=tool_call_identifier, tool=tool_name, code="invalid_location", message=str(exception))
                return
            candidate = raw_target[len("file://"):] if lowered.startswith("file://") else raw_target
            resolved_path = await asyncio.to_thread(resolved_location.executor.resolve, resolved_location.base_directory, candidate)
            if not await asyncio.to_thread(resolved_location.executor.exists, resolved_path):
                yield StreamEvent(
                    StreamEvent.Type.ERROR, id=tool_call_identifier, tool=tool_name,
                    code="artifact_file_not_found",
                    message=f"No file to open at {resolved_path}. Write the file first, then open it.",
                )
                return
            kind = artifact_kind_for(resolved_path)
            if kind == "file":
                # A code or text file has no visual form, so opening it as an artifact would just
                # show an empty panel. The artifacts panel is a preview surface; keep it for
                # things that render.
                yield StreamEvent(
                    StreamEvent.Type.ERROR, id=tool_call_identifier, tool=tool_name,
                    code="artifact_not_previewable",
                    message=self._prompt_loader.load(
                        "artifact_not_previewable", {"file_name": Path(resolved_path).name},
                    ).strip(),
                )
                return
            display_title = Path(resolved_path).name or resolved_path
            artifact_id = requested_artifact_id or self._artifact_surface_id(f"{resolved_location.uri}:{resolved_path}")
            self._capture_written_artifacts(
                resolved_location, changed_absolute_paths=[resolved_path],
                tool_call_id=tool_call_identifier, message=f"open_artifact {display_title}",
                surface={"surface_id": artifact_id, "kind": kind, "title": display_title, "source": resolved_path, "absolute_path": resolved_path},
            )
            result = build_open_artifact_result(
                artifact_id=artifact_id, kind=kind, title=display_title, source=resolved_path,
                location_uri=resolved_location.uri, absolute_path=resolved_path,
                height=requested_height,
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
            elif requested_task_id in self._background_agent_results:
                bg_result = self._background_agent_results[requested_task_id]
                if bg_result is None:
                    result = {"code": "running", "task_id": requested_task_id, "message": "Agent is still running."}
                else:
                    result = bg_result
            else:
                task = await self._task_reader(requested_task_id)
                if task is None:
                    result = {"code": "task_not_found", "task_id": requested_task_id}
                else:
                    result = task
            yield StreamEvent(StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=result)

        elif tool_name == "computer":
            from harness.computer import engine as computer_engine, permissions as computer_permissions
            action = str(tool_arguments.get("action", ""))
            # The AX-tree actions need the Accessibility grant; scripting/launch do not.
            # A clear, actionable error beats a silent empty result.
            if action in ("observe", "click", "type", "key", "menu", "scroll") and not computer_permissions.accessibility_granted():
                yield StreamEvent(
                    StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name,
                    result={
                        "ok": False,
                        "error": "Accessibility permission is not granted for this app, so it cannot read or control other apps. Grant it in System Settings › Privacy & Security › Accessibility.",
                        "needs_permission": "accessibility",
                    },
                )
                return

            def _run_computer() -> dict:
                app = str(tool_arguments.get("app", ""))
                element = tool_arguments.get("element")
                text = str(tool_arguments.get("text", ""))
                window = str(tool_arguments.get("window", "focused") or "focused")
                language = str(tool_arguments.get("language", "applescript") or "applescript")
                clicks = int(tool_arguments.get("clicks", 1) or 1)
                button = str(tool_arguments.get("button", "left") or "left")
                key = str(tool_arguments.get("key", ""))
                modifiers = tool_arguments.get("modifiers") or []
                menu_path = tool_arguments.get("menu_path") or []
                direction = str(tool_arguments.get("direction", ""))
                arguments = tool_arguments.get("arguments") or []
                if action == "observe":
                    return computer_engine.observe(app, window, element=int(element) if element is not None else None)
                if action in ("click", "type"):
                    if element is None:
                        return {"ok": False, "error": f"The {action} action needs an element index from the last observe."}
                    if action == "click":
                        return computer_engine.click(int(element), clicks=clicks, button=button)
                    return computer_engine.set_text(int(element), text)
                if action == "key":
                    return computer_engine.press_key(key, modifiers, app)
                if action == "menu":
                    return computer_engine.open_menu(menu_path, app)
                if action == "scroll":
                    return computer_engine.scroll(direction or "down", target=app)
                if action == "screenshot":
                    return computer_engine.screenshot(app)
                if action == "run_script":
                    return computer_engine.run_script(text, language)
                if action == "launch":
                    return computer_engine.launch(app, arguments)
                return {"ok": False, "error": f"Unknown action {action!r}."}

            result = await asyncio.to_thread(_run_computer)
            extra: dict[str, Any] = {}
            # A screenshot's pixels ride the model_image side channel to a vision-capable
            # model, exactly like read_file; the raw path never reaches the UI.
            if action == "screenshot" and isinstance(result, dict) and result.get("image_path"):
                if self._model_supports_vision():
                    data_uri = await asyncio.to_thread(_screenshot_data_uri, result["image_path"])
                    if data_uri:
                        extra["model_image"] = data_uri
                result.pop("image_path", None)
            yield StreamEvent(
                StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=result, **extra,
            )

        elif tool_name == "browser":
            from harness.computer import web as browser_bridge
            action = str(tool_arguments.get("action", ""))

            def _run_browser() -> dict:
                url = str(tool_arguments.get("url", ""))
                element = tool_arguments.get("element")
                text = str(tool_arguments.get("text", ""))
                key = str(tool_arguments.get("key", ""))
                direction = str(tool_arguments.get("direction") or "down")
                tab = str(tool_arguments.get("tab", ""))
                browser_name = str(tool_arguments.get("browser_name") or "chrome")
                if action == "navigate":
                    if not url:
                        return {"ok": False, "error": "The navigate action needs a url."}
                    return browser_bridge.navigate(url, browser=browser_name)
                if action == "observe":
                    return browser_bridge.observe(element=int(element) if element is not None else None)
                if action == "find":
                    query = str(tool_arguments.get("query", ""))
                    if not query.strip():
                        return {"ok": False, "error": "The find action needs a query — the text to look for on the page."}
                    return browser_bridge.find(query)
                if action == "screenshot":
                    return browser_bridge.screenshot()
                if action in ("click", "type", "hover", "select", "upload", "drag"):
                    if element is None:
                        return {"ok": False, "error": f"The {action} action needs an element index from the last observe."}
                    if action == "click":
                        return browser_bridge.click(int(element))
                    if action == "hover":
                        return browser_bridge.hover(int(element))
                    if action == "select":
                        option = str(tool_arguments.get("option", ""))
                        if not option:
                            return {"ok": False, "error": "The select action needs an option — the visible label (or value) to choose."}
                        return browser_bridge.select_option(int(element), option)
                    if action == "upload":
                        paths = tool_arguments.get("paths") or []
                        if isinstance(paths, str):
                            paths = [paths]
                        if not paths:
                            return {"ok": False, "error": "The upload action needs paths — the local file(s) to attach."}
                        return browser_bridge.upload(int(element), [str(path) for path in paths])
                    if action == "drag":
                        to_element = tool_arguments.get("to_element")
                        if to_element is None:
                            return {"ok": False, "error": "The drag action needs to_element — the index to drop onto."}
                        return browser_bridge.drag(int(element), int(to_element))
                    return browser_bridge.type_text(int(element), text, submit=bool(tool_arguments.get("submit", False)))
                if action == "press":
                    if not key:
                        return {"ok": False, "error": "The press action needs a key (e.g. Enter)."}
                    return browser_bridge.press(key)
                if action == "scroll":
                    return browser_bridge.scroll(direction, element=int(element) if element is not None else None)
                if action == "read":
                    return browser_bridge.read(
                        offset=int(tool_arguments.get("offset", 0) or 0),
                        element=int(element) if element is not None else None,
                    )
                if action == "back":
                    return browser_bridge.history_back()
                if action == "forward":
                    return browser_bridge.history_forward()
                if action == "reload":
                    return browser_bridge.reload()
                if action == "tabs":
                    return browser_bridge.list_tabs()
                if action == "new_tab":
                    return browser_bridge.new_tab(url, browser=browser_name)
                if action in ("switch_tab", "close_tab"):
                    if not tab:
                        return {"ok": False, "error": f"The {action} action needs a tab id from the tabs action."}
                    if action == "switch_tab":
                        return browser_bridge.switch_tab(tab)
                    return browser_bridge.close_tab(tab)
                return {"ok": False, "error": f"Unknown action {action!r}."}

            result = await asyncio.to_thread(_run_browser)
            extra = {}
            # A page screenshot's pixels ride the model_image side channel to a vision-capable
            # model, exactly like the computer tool's. The raw path never reaches the UI.
            if action == "screenshot" and isinstance(result, dict) and result.get("image_path"):
                if self._model_supports_vision():
                    data_uri = await asyncio.to_thread(_screenshot_data_uri, result["image_path"])
                    if data_uri:
                        extra["model_image"] = data_uri
                result.pop("image_path", None)
            yield StreamEvent(
                StreamEvent.Type.TOOL_RESULT, id=tool_call_identifier, name=tool_name, result=result, **extra,
            )

        else:
            yield StreamEvent(
                StreamEvent.Type.ERROR, id=tool_call_identifier,
                message=f"Unknown tool '{tool_name}'", tool=tool_name,
            )

    def get_execution_history(self) -> list[dict]:
        return self._execution_history
