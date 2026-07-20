import asyncio
import json
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any, Callable, Optional

from langchain_core.messages import (
    AIMessage,
)
from langchain_core.tools import BaseTool
from pydantic import BaseModel, SecretStr



from harness.core.configuration import (
    AgentConfiguration,
    GlobalConfiguration,
    PermissionEvaluator,
    PromptLoader,
)
from langchain_core.language_models.chat_models import BaseChatModel
from harness.core.litellm_model import ChatLiteLLMModel
from harness.core.codex_model import ChatCodexModel
from harness.core.agent_messages import AgentMessage
from harness.core.file_leases import FileLeaseManager
from harness.core.models import find_model, resolve_litellm
from harness.locations.resolver import LocationAddress, executor_for, location_uri_for
from harness.tools.tools import (
    bash as bash_tool,
    web_search as web_search_tool,
    spawn_agent as spawn_tool,
    call_remote_agent as call_remote_agent_tool,
    cancel_agent as cancel_agent_tool,
    ask_agent as ask_agent_tool,
    respond_agent as respond_agent_tool,
    read_task as read_task_tool,
    set_tasks as set_tasks_tool,
    update_tasks as update_tasks_tool,
    update_goal as update_goal_tool,
    open_artifact as open_artifact_tool,
    list_mcp_tools as list_mcp_tools_tool,
    call_mcp_tool as call_mcp_tool_tool,
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
    wait_for as wait_for_tool,
)
from harness.core.background import (
    BackgroundJobs,
    background_completion_event,
    background_include_result,
)


from harness.core.tool_policy import (
    CallExecutionPolicy,
    PermissionMode,
    ResolvedLocation,
    ToolLocationError,
)
from harness.core.turn_events import (
    ToolResult,
    TurnEvent,
    Usage,
)

from harness.core.agent_tools import (
    _ToolsMixin,
)

from harness.core.agent_delegation import (
    _DelegationMixin,
)

from harness.core.agent_turnloop import (
    _TurnLoopMixin,
)

from harness.core.agent_permissions import (
    _PermissionsMixin,
)

from harness.core.agent_compaction import (
    _CompactionMixin,
)


from harness.core.agent_internals import (
    _cap_model_result_payload,
    _maybe_json,
    _model_result_status,
    _model_visible_tool_result,
    _tool_timing_metadata,
    _utc_timestamp,
    model_is_authorized,
)


def build_chat_model(
    model_identifier: str,
    global_configuration: "GlobalConfiguration",
    agent_configuration: "AgentConfiguration",
) -> BaseChatModel:
    """Build the chat model for a provider-qualified (``provider/model``) id.

    Almost every provider flows through one ``ChatLiteLLMModel`` (LiteLLM owns each
    provider's auth, base URL, request format, and reasoning normalization). The one
    exception is the experimental ``chatgpt`` subscription provider, which is not a
    LiteLLM route at all: it uses its own ``ChatCodexModel``, reading its OAuth token
    from the shared token store (no api_key/api_base) and calling Codex's Responses
    endpoint directly."""
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
    return ChatLiteLLMModel.model_validate({
        "model": resolved["model"],
        "api_key": SecretStr(resolved["api_key"]) if resolved["api_key"] else None,
        "api_base": resolved["api_base"] or None,
        "temperature": 0,
        "reasoning_effort": agent_configuration.reasoning_effort,
    })


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
        wait_for_tool,
        web_search_tool,
        set_tasks_tool,
        update_tasks_tool,
        update_goal_tool,
        read_task_tool,
        ask_agent_tool,
        respond_agent_tool,
        # A delegated agent can ask the user directly: the question parks the delegated turn and
        # is propagated to the panel/overlay like any human-in-the-loop gate, then resumes
        # on the answer. (open_artifact stays top-level only — it drives the user's UI.)
        ask_user_tool,
    ]
    if not is_agent:
        available.append(open_artifact_tool)
    if agent_configuration.tools.spawn_agent.enabled:
        available.append(spawn_tool)
        available.append(cancel_agent_tool)
        # A distinct tool for external A2A agents, offered only when this profile is
        # allowed at least one configured remote agent.
        profile = agent_configuration.identifier
        if any(
            not remote.allowed_profiles or profile in remote.allowed_profiles
            for remote in global_configuration.remote_agents.enabled_agents().values()
        ):
            available.append(call_remote_agent_tool)
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


class TaskItem(BaseModel):
    identifier: str = ""
    description: str
    status: str = "pending"
    dependencies: list[str] = []


class TaskManager:
    def __init__(self):
        self._tasks: list[TaskItem] = []
        self._next_identifier: int = 1

    def add_tasks(self, task_definitions: list[dict]) -> list[str]:
        created = []
        for definition in task_definitions:
            identifier = f"task-{self._next_identifier}"
            self._next_identifier += 1
            task = TaskItem(
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

    def snapshot(self) -> dict:
        """The manager's full durable state — the task list plus the id counter — so a
        rebuilt runtime restores identical tasks and keeps minting non-colliding ids."""
        return {"tasks": self.to_dict_list(), "next_identifier": self._next_identifier}

    def restore(self, snapshot: dict) -> None:
        """Rehydrate from :meth:`snapshot`. Tolerates a missing or partial snapshot (a
        session that never set tasks) by leaving the manager empty."""
        self._tasks = [TaskItem.model_validate(task) for task in snapshot.get("tasks", [])]
        self._next_identifier = int(snapshot.get("next_identifier", len(self._tasks) + 1))


class AgentRuntime(_ToolsMixin, _PermissionsMixin, _CompactionMixin, _DelegationMixin, _TurnLoopMixin):
    # A turn runs until the model is done or the user interrupts it — there is no tool-call
    # ceiling and no heuristic stuck-detector. The model owns progress: it ends its own turn
    # when finished, uses ``wait_for`` to poll rather than spinning, and re-reads a tool's
    # ``output_file`` to see whether a repeated action changed anything. Context compaction is
    # Observational Memory (Observer/Reflector); its thresholds and on/off switch live in
    # GlobalConfiguration.compaction.

    # Tool-name -> handler method. ``_execute_tool`` resolves permission, location, and
    # policy once (the shared preamble), then dispatches the call to its handler here.
    # Grouped tools (edit/write, spawn/remote, the MCP queries, computer/browser) share
    # one handler; an unmapped name is the "unknown tool" error.
    _TOOL_HANDLERS = {
        "bash": "_tool_bash",
        "read_file": "_tool_read_file",
        "find_files": "_tool_find_files",
        "search_content": "_tool_search_content",
        "fetch_url": "_tool_fetch_url",
        "download_file": "_tool_download_file",
        "edit_file": "_tool_edit_or_write",
        "write_file": "_tool_edit_or_write",
        "load_skill": "_tool_load_skill",
        "wait_for": "_tool_wait_for",
        "ask_user": "_tool_ask_user",
        "call_mcp_tool": "_tool_call_mcp_tool",
        "list_mcp_tools": "_tool_mcp_query",
        "list_mcp_resources": "_tool_mcp_query",
        "read_mcp_resource": "_tool_mcp_query",
        "ask_agent": "_tool_ask_agent",
        "respond_agent": "_tool_respond_agent",
        "spawn_agent": "_tool_spawn_or_remote",
        "call_remote_agent": "_tool_spawn_or_remote",
        "cancel_agent": "_tool_cancel_agent",
        "set_tasks": "_tool_set_tasks",
        "update_tasks": "_tool_update_tasks",
        "update_goal": "_tool_update_goal",
        "open_artifact": "_tool_open_artifact",
        "web_search": "_tool_web_search",
        "read_task": "_tool_read_task",
        "computer": "_tool_automation",
        "browser": "_tool_automation",
    }

    def __init__(
        self,
        agent_configuration: AgentConfiguration,
        global_configuration: GlobalConfiguration,
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
        self._build_locations(
            locations, permission_mode_default=agent_configuration.permission_policy
        )

        effective_model = agent_configuration.model_identifier
        if not effective_model:
            raise ValueError(
                f"Agent '{agent_configuration.identifier}' must configure both provider and model."
            )
        # When this agent's own provider isn't authorized — the common case for a
        # delegation-target profile still pinned to a provider the user never keyed
        # (e.g. a shipped `opencode/*` default after the session switched to the
        # ChatGPT subscription) — building its client anyway yields a model that
        # 401s on its first call, so a spawned agent dies the instant it starts.
        # Fall back to the default agent's authorized model so the delegated agent inherits
        # the session's working model instead. This is a no-op for the default agent
        # itself (its model is already what we'd fall back to).
        if not model_is_authorized(effective_model, global_configuration):
            fallback_model = self._authorized_default_model()
            if fallback_model:
                effective_model = fallback_model
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
        # Set when the goal or task list changes, so the executor persists the durable
        # session state only on mutation rather than on every checkpoint.
        self._session_dirty = False
        self._execution_history: list[dict] = []
        # The runtime's effective permission policy is ONE typed value (the agent card's
        # configured mode until a session or delegation override changes it); the read_only/
        # bypass/auto booleans the call sites read are derived views of it, never separate
        # state that could drift. `_session_permission_mode` records the live override so a
        # location's own mode governs its calls only while the session is on the default.
        self._permission_mode: PermissionMode = agent_configuration.permission_policy
        self._session_permission_mode: PermissionMode = PermissionMode.DEFAULT
        # A delegated agent parks on these while its human-in-the-loop request is escalated to
        # the user; the resolver (native REST or an A2A input_response) completes them.
        self._agent_permission_futures: dict[str, asyncio.Future] = {}
        # Durably persists a delegated agent's 'always allow' as allow-patterns on its own agent
        # profile's configuration: async (agent_identifier, project_directory, patterns).
        # Injected by the executor; a delegated agent has no session to remember the rule in.
        self._persist_agent_allow_patterns: Optional[Callable[..., Any]] = None
        # When set, agents (spawn_agent calls) are invoked
        # through this delegate — an A2A call to the target agent's served
        # endpoint — instead of being run in-process. Bound to the A2A context.
        self._delegate: Optional[Callable] = None
        # Cancels a running agent's own A2A task when its public agent handle is
        # explicitly canceled. The executor injects the async callback.
        self._cancel_delegated: Optional[Callable] = None
        self._a2a_task_id: str = ""
        self._ask_agent: Optional[Callable[[str, str, str], dict[str, Any]]] = None
        self._respond_agent: Optional[Callable[[str, str, str], dict[str, Any]]] = None
        self._reserve_agent: Optional[Callable[[str, str, str], None]] = None
        self._release_reserved_agent: Optional[Callable[[str], None]] = None
        self._active_agents: Optional[Callable[[str], list[dict[str, str]]]] = None
        # External (over-the-wire) A2A agents the model may delegate to. The registry
        # supplies a roster (so they appear in the system prompt alongside local agents)
        # and a predicate (so the spawn path resolves a remote name over the wire via the
        # delegate instead of trying to load an on-disk config that does not exist).
        self._remote_agent_roster: Callable[[], list[dict[str, str]]] = lambda: []
        self._is_remote_agent: Callable[[str], bool] = lambda name: False
        # The current turn's file attachments, forwarded to a remote agent as FileParts.
        self._pending_attachments: list[dict] = []
        # Remote agents the user has approved contacting for the rest of this session, so
        # egress consent is asked once per agent, not on every call.
        self._egress_approved_agents: set[str] = set()
        self._agent_messages: asyncio.Queue[AgentMessage] = asyncio.Queue()
        self._agent_message_available = asyncio.Event()
        self._pending_agent_questions: set[str] = set()
        self._outstanding_agent_questions: set[str] = set()
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
        # Live activity from non-blocking spawned agents. spawn_agent returns
        # immediately (the agent runs as a background job); the agent's
        # streamed events land here and the turn loop drains them so the agents
        # panel updates while the parent keeps working. Whatever is still queued
        # when the parent goes idle drains on the next (wake) turn.
        self._spawned_agent_events: "asyncio.Queue[TurnEvent]" = asyncio.Queue()
        self._agent_event_sink: Optional[Callable[[dict[str, Any]], None]] = None
        self._settled_agent_lanes: set[tuple[str, str]] = set()
        # The latest call's context occupancy (prompt + completion) and the model's
        # window, tracked from usage so auto-compaction can fire before the next call
        # would overflow. Zero until the first call reports usage.
        self._latest_context_tokens: int = 0
        self._context_window: int = 0

    def _build_locations(self, locations: list[dict] | None, *, permission_mode_default: PermissionMode) -> None:
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
                "permission_mode": str(permission_mode_default),
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
                permission_mode=PermissionMode.coerce(entry.get("permission_mode"), permission_mode_default),
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
        an explicit live session mode governs every target immediately. While the session
        remains on ``default``, a target's explicit mode governs its calls, then the agent
        profile is the fallback. A runtime-level read-only override is always a floor.
        Returned as a value and threaded through the call, never written to shared state,
        so concurrent calls to different locations cannot cross policies."""
        session_mode_is_explicit = self._session_permission_mode is not PermissionMode.DEFAULT
        if location is None or session_mode_is_explicit or location.permission_mode is PermissionMode.DEFAULT:
            # No location to govern the call, an explicit live session mode, or a location left
            # on the default — the runtime's own mode applies.
            mode = self._permission_mode
        elif self._permission_mode is PermissionMode.READ_ONLY:
            # A runtime-level read-only override is a floor no location mode can lift.
            mode = PermissionMode.READ_ONLY
        else:
            # Otherwise the location's explicit mode governs its own calls.
            mode = location.permission_mode
        working_directory = (
            self._working_directory
            if location is None or location.is_remote
            else location.base_directory
        )
        return CallExecutionPolicy(location=location, working_directory=working_directory, mode=mode)

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

    def has_pending_jobs(self) -> bool:
        """Whether any background job is still in flight — the scheduling predicate the
        executor's resume pump reads, exposed here so it does not reach through into the
        job runner's internals."""
        return self._background.has_pending()

    def has_completed_undelivered_jobs(self) -> bool:
        """Whether a completed background result is waiting to be delivered to the model."""
        return self._background.has_completed_undelivered()

    async def wait_for_jobs(self) -> None:
        """Await the next background-job completion (the resume pump's wait point)."""
        await self._background.wait_for_completion()

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

    def _accumulate_usage(self, response: AIMessage) -> "TurnEvent | None":
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
        return Usage(input_tokens=input_tokens,
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

    # Derived views of the single `_permission_mode`, so the many call sites that ask a plain
    # boolean keep working while there is exactly one source of truth behind them.
    @property
    def _read_only(self) -> bool:
        return self._permission_mode.is_read_only

    @property
    def _bypass_permissions(self) -> bool:
        return self._permission_mode.is_bypass

    @property
    def _auto_permissions(self) -> bool:
        return self._permission_mode.is_auto

    def abort(self) -> None:
        # Stop tears down only the live turn: signal the loop to end and kill every
        # foreground tool still running. Detached background work and spawned agents
        # have independent lifecycles and must not become collateral of steering the
        # main flow; each can be canceled through its own targeted control.
        self._abort_event.set()
        self._background.cancel_foreground()
        for task in list(self._active_tool_tasks.values()):
            task.cancel()

    def abort_tool(self, tool_call_identifier: str) -> bool:
        task = self._active_tool_tasks.get(tool_call_identifier)
        aborted = False
        if task is not None and not task.done():
            task.cancel()
            aborted = True
        return self._background.cancel_by_tool_call(tool_call_identifier) or aborted

    def cancel_agent(self, task_identifier: str) -> bool:
        """Cancel one spawned agent by its public agent handle."""
        return self._background.cancel_by_identifier(task_identifier, kind="spawn_agent")

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
        """Force (or release) a read-only floor over the runtime's mode — the override a
        spawning call/step applies. Turning it on makes the mode read-only outright; turning
        it off drops a read-only floor back to the interactive default but leaves any other
        mode untouched."""
        if read_only:
            self._permission_mode = PermissionMode.READ_ONLY
        elif self._permission_mode is PermissionMode.READ_ONLY:
            self._permission_mode = PermissionMode.DEFAULT

    def set_permission_mode(self, mode: "str | PermissionMode") -> None:
        """Apply a live session override. ``default`` restores the agent card's own configured
        mode; any other known mode replaces it. An unknown value is ignored."""
        parsed = PermissionMode.parse(mode)
        if parsed is None:
            return
        self._session_permission_mode = parsed
        self._permission_mode = (
            self._agent_configuration.permission_policy
            if parsed is PermissionMode.DEFAULT
            else parsed
        )

    @property
    def configured_permission_mode(self) -> PermissionMode:
        """The permission mode the agent's own card declares (its ceiling before a
        caller's grant tightens it)."""
        return self._agent_configuration.permission_policy

    def resolve_agent_permission(self, request_id: str, value: Any) -> bool:
        """Complete a delegated agent's parked human-in-the-loop request with the user's answer
        (a decision string for a permission, or the answers / decline for a question).
        Returns whether a matching pending request was found."""
        future = self._agent_permission_futures.get(request_id)
        if future is not None and not future.done():
            future.set_result(value)
            return True
        return False

    def set_delegated_policy(self, mode: str) -> None:
        """Apply a delegated agent's effective permission policy. Unlike ``set_permission_mode``,
        "default" means the interactive (ask) policy — never the agent's own configured
        mode — and ``bypass`` is never applied: a delegated agent can never run unattended. Under
        the interactive policy an unmatched command asks (escalates to the user)."""
        parsed = PermissionMode.parse(mode)
        # bypass is never applied to a delegated agent, and "default" means the interactive
        # policy here (never the agent's own configured mode).
        if parsed not in (PermissionMode.READ_ONLY, PermissionMode.AUTO):
            parsed = PermissionMode.DEFAULT
        self._session_permission_mode = parsed
        self._permission_mode = parsed

    def set_delegate(self, delegate: Callable) -> None:
        """Install the A2A delegate used to invoke agents as related tasks."""
        self._delegate = delegate

    def set_cancel_delegated(self, cancel_delegated: Callable) -> None:
        """Install the callback used by targeted spawned-agent cancellation."""
        self._cancel_delegated = cancel_delegated

    def set_persist_agent_allow_patterns(self, persist: Callable[..., Any]) -> None:
        """Install the callback that durably records a delegated agent's 'always allow' as
        allow-patterns on its agent profile's configuration."""
        self._persist_agent_allow_patterns = persist

    def set_remote_agents(
        self,
        roster: Callable[[], list[dict[str, str]]],
        is_remote: Callable[[str], bool],
    ) -> None:
        """Install the external A2A agent roster and predicate. Remote agents appear in
        the model's roster like local ones, but the spawn path resolves them over the wire
        (through the delegate) rather than loading an on-disk agent config."""
        self._remote_agent_roster = roster
        self._is_remote_agent = is_remote

    def set_pending_attachments(self, attachments: list[dict]) -> None:
        """Record the current turn's file attachments so a delegation to a remote agent can
        forward them as FileParts."""
        self._pending_attachments = attachments or []

    def set_agent_event_sink(self, agent_event_sink: Callable[[dict[str, Any]], None]) -> None:
        """Install the immediate delivery path for path-tagged agent activity."""
        self._agent_event_sink = agent_event_sink

    def set_agent_messaging(
        self,
        ask_agent: Callable[[str, str, str], dict[str, Any]],
        respond_agent: Callable[[str, str, str], dict[str, Any]],
        reserve_agent: Callable[[str, str, str], None],
        release_reserved_agent: Callable[[str], None],
        active_agents: Callable[[str], list[dict[str, str]]],
    ) -> None:
        """Install the active-task mailbox operations owned by the agent registry."""
        self._ask_agent = ask_agent
        self._respond_agent = respond_agent
        self._reserve_agent = reserve_agent
        self._release_reserved_agent = release_reserved_agent
        self._active_agents = active_agents

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
        """Record how many delegation hops led to this runtime, for context and telemetry
        (there is no delegation-depth ceiling; recursion is governed by the model's own
        judgment and the user's ability to interrupt)."""
        self._delegation_depth = depth

    def session_snapshot(self) -> dict:
        """The context's durable non-conversation state — the active goal and the task
        list — persisted alongside the conversation checkpoint so a restart restores the
        agent's objective, not just its transcript."""
        return {"goal": self._active_goal, "tasks": self._task_manager.snapshot()}

    def restore_session(self, snapshot: dict) -> None:
        """Rehydrate goal and tasks from :meth:`session_snapshot` when a context is rebuilt
        (e.g. a session reopened after a restart). A missing snapshot leaves both empty."""
        self._active_goal = str(snapshot.get("goal", ""))
        self._task_manager.restore(snapshot.get("tasks", {}) or {})

    def dirty_session_snapshot(self) -> Optional[dict]:
        """The session snapshot if the goal or tasks changed since the last persist, else
        ``None`` — so the executor writes durable session state on mutation only, not on every
        safe point. This only *peeks*: the dirty flag is cleared by :meth:`clear_session_dirty`
        after the write commits, so a failed or crashed write never loses the mutation."""
        return self.session_snapshot() if self._session_dirty else None

    def clear_session_dirty(self) -> None:
        """Mark the durable session state as persisted — called only after the atomic
        checkpoint+session-state write succeeds, so the dirty flag is a write-then-clear."""
        self._session_dirty = False

    @property
    def _interactive_manual_mode(self) -> bool:
        """The interactive ("manual") permission policy: not auto-classifying, not
        read-only, not bypass. Under it, a command the card does not explicitly allow is
        asked (escalated to the user) rather than run — for the top-level agent and a
        delegated agent alike. Auto self-classifies, read-only hard-blocks mutations, and bypass
        allows everything, so none of those ask on an unmatched command."""
        return self._permission_mode.is_interactive

    def _record_event(self, event_type: str, data: dict) -> None:
        record = {"type": event_type, "timestamp": _utc_timestamp(datetime.now(timezone.utc)), **data}
        self._execution_history.append(record)
        if self._on_record_event:
            self._on_record_event(event_type, data)

    def _record_message(self, role: str, content: str, tool_call_id: str = "") -> None:
        if self._on_record_message:
            self._on_record_message(role, content, tool_call_id)

    def _background_result_events(self) -> list[TurnEvent]:
        events: list[TurnEvent] = []
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
            events.append(ToolResult(id=completion.tool_call_identifier,
                name=completion.kind,
                result=_maybe_json(capped_result),
                status=background_status,
                task_id=completion.identifier,
            ))
            completion_event_data: dict[str, Any] = {"task_identifier": completion.identifier}
            if background_include_result(completion.kind):
                completion_event_data["result"] = capped_result
            self._record_event(background_completion_event(completion.kind), completion_event_data)
        return events

    def _model_supports_vision(self) -> bool:
        """Whether the agent's model advertises image input. Unknown models (a
        custom endpoint not in the catalog) are assumed vision-capable — the
        same permissive default the attachment-inlining path uses."""
        model = find_model(self._effective_model_identifier)
        return True if model is None else model.vision

    def get_execution_history(self) -> list[dict]:
        return self._execution_history
