from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from langchain_core.messages import (
    AIMessage,
    messages_to_dict,
)
from langchain_core.tools import BaseTool
from pydantic import BaseModel, SecretStr, ValidationError


from langmesh.base.configuration import (
    AgentConfiguration,
    Configuration,
    PermissionEvaluator,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langmesh.runtime.models.litellm import ChatLiteLLMModel
from langmesh.runtime.models.codex import ChatCodexModel
from langmesh.runtime.models.cursor import ChatCursorModel
from langmesh.base.file_leases import FileLeaseManager
from langmesh.base.models import find_model, resolve_litellm
from langmesh.locations.resolver import LocationAddress, executor_for, location_uri_for
from langmesh.runtime.tools.registry import (
    bash as bash_tool,
    search_web as search_web_tool,
    read_turn as read_turn_tool,
    set_tasks as set_tasks_tool,
    update_tasks as update_tasks_tool,
    update_goal as update_goal_tool,
    list_mcp_tools as list_mcp_tools_tool,
    call_mcp_tool as call_mcp_tool_tool,
    list_mcp_resources as list_mcp_resources_tool,
    read_mcp_resource as read_mcp_resource_tool,
    fetch_url as fetch_url_tool,
    download_file as download_file_tool,
    control_screen as control_screen_tool,
    ask_user as ask_user_tool,
    load_skill as load_skill_tool,
    wait_for as wait_for_tool,
)
from langmesh.base.ports import Observation
from langmesh.runtime.tools.context import ToolContext
from langmesh.runtime.tools.sessions import remote_agent_tools, session_tools
from langmesh.runtime.background import (
    BackgroundJobs,
    background_completion_event,
    background_include_result,
)


from langmesh.base.permission_mode import PermissionMode

from langmesh.runtime.locations import CallExecutionPolicy, ResolvedLocation, ToolLocationError
from langmesh.runtime.turn_events import (
    ToolResult,
    TurnEvent,
    Usage,
)

from langmesh.runtime.tools.dispatch import (
    _DispatchesTools,
)

from langmesh.runtime.turn import (
    _RunsTurns,
)

from langmesh.runtime.permissions import (
    _DecidesPermissions,
)

from langmesh.runtime.compaction import (
    _CompactsContext,
)
from langmesh.base.serialization import compact
from langmesh.base.toolbox import toolbox_for
from langmesh.runtime.goal import Goal
from langmesh.runtime.internals import (
    _cap_model_result_payload,
    _maybe_json,
    _model_result_status,
    _model_visible_tool_result,
    _tool_timing_metadata,
    _utc_timestamp,
)


logger = logging.getLogger(__name__)


class _CataloguePrompts:
    """A `PromptLoader`-shaped view of a catalogue, so the template seam cost one adapter rather than a rewrite."""

    def __init__(self, catalogue: Any) -> None:
        self._catalogue = catalogue

    def load(self, template_name: str, variables: dict[str, str]) -> str:
        return self._catalogue.prompt(template_name, variables)


async def _drain_observation(pending) -> None:
    """Await an observer's awaitable and swallow it, since nothing else ever retrieves its exception."""
    try:
        await pending
    except Exception:  # noqa: BLE001 — an audit sink must never fail a turn
        logger.debug("an asynchronous observer raised", exc_info=True)


def build_chat_model(
    model_identifier: str,
    global_configuration: Configuration,
    agent_configuration: AgentConfiguration,
    working_directory: str,
    session_id: str = "",
) -> BaseChatModel:
    """Build the chat model for a ``provider/model`` id: LiteLLM for almost all, and the two OAuth providers apart."""
    provider_identifier, model_suffix = model_identifier.split("/", 1)
    if provider_identifier == "chatgpt":
        catalog_entry = find_model(model_identifier)
        return ChatCodexModel(
            model=model_suffix,
            reasoning_effort=agent_configuration.reasoning_effort,
            context_length=catalog_entry.context_length if catalog_entry else 0,
            session_id=session_id,
        )
    if provider_identifier == "cursor":
        catalog_entry = find_model(model_identifier)
        # No reasoning_effort: a Cursor model id carries its effort, so a second setting could only disagree.
        return ChatCursorModel(
            model=model_suffix,
            workspace=working_directory,
            context_length=catalog_entry.context_length if catalog_entry else 0,
        )
    resolved = resolve_litellm(
        model_identifier,
        global_configuration.configured_provider_keys(),
        global_configuration.configured_provider_bases(),
    )
    # The catalogue's window travels with the model, since LiteLLM knows nothing of a gateway's models.
    catalogued = find_model(model_identifier)
    return ChatLiteLLMModel.model_validate({
        "model": resolved["model"],
        "api_key": SecretStr(resolved["api_key"]) if resolved["api_key"] else None,
        "api_base": resolved["api_base"] or None,
        "session_id": session_id,
        "context_length": catalogued.context_length if catalogued else 0,
        "temperature": 0,
        "reasoning_effort": agent_configuration.reasoning_effort,
    })


def _build_tools(
    agent_configuration: AgentConfiguration,
    global_configuration: Configuration,
    working_directory: str = "",
    *,
    can_reach_peers: bool = False,
    extra_tools: Sequence[BaseTool] = (),
    permission_mode: PermissionMode = PermissionMode.ASK,
) -> list[BaseTool]:
    tools = _all_available_tools(
        agent_configuration, global_configuration, working_directory,
        can_reach_peers=can_reach_peers, extra_tools=extra_tools,
        permission_mode=permission_mode,
    )
    # A profile's allow-list narrows our tools and never the caller's, which it was written long before.
    supplied = {tool.name for tool in extra_tools}
    allowed = _live_allow_list(
        agent_configuration.tools_enabled, {tool.name for tool in tools} - supplied,
    )
    # `disabled` applies to ours and the caller's alike: switching a supplied tool off means it.
    return [
        tool for tool in tools
        if agent_configuration.tools.is_enabled(tool.name)
        and (tool.name in supplied or not allowed or tool.name in allowed)
    ]


def _live_allow_list(configured: list[str], existing: set[str]) -> set[str]:
    """An agent's allow-list narrowed to tools that exist, so a stale name cannot leave it permitting nothing."""
    live = {name for name in configured if name in existing}
    return live


def _installed_agent_names(global_configuration: Configuration, working_directory: str) -> list[str]:
    """The profiles a peer could be created with, read at build time so a bad name is unrepresentable."""
    from langmesh.base.configuration import list_agents

    directories = (
        global_configuration.agent_directories_for(working_directory)
        if working_directory
        else global_configuration.agent_directories()
    )
    try:
        return [entry["id"] for entry in list_agents(directories)]
    except Exception:  # noqa: BLE001 — an unreadable profile directory must not fail the runtime
        return []


def _all_available_tools(
    agent_configuration: AgentConfiguration,
    global_configuration: Configuration,
    working_directory: str = "",
    *,
    can_reach_peers: bool = False,
    extra_tools: Sequence[BaseTool] = (),
    permission_mode: PermissionMode = PermissionMode.ASK,
) -> list[BaseTool]:
    available = [
        bash_tool,
        fetch_url_tool,
        download_file_tool,
        load_skill_tool,
        wait_for_tool,
        search_web_tool,
        set_tasks_tool,
        update_tasks_tool,
        update_goal_tool,
        read_turn_tool,
    ]
    # Asking parks the turn, which only makes sense where somebody is there — so `automatic` is not given the tool.
    if permission_mode.asks:
        available.append(ask_user_tool)
    # Driving the live screen is opt-in, added only where the user enabled it in Settings.
    if global_configuration.computer_control.enabled:
        available.append(control_screen_tool)
    if global_configuration.mcp.enabled_servers():
        available.extend([
            list_mcp_tools_tool,
            call_mcp_tool_tool,
            list_mcp_resources_tool,
            read_mcp_resource_tool,
        ])
    # Peer sessions: offered only with a profile to run and a control plane to reach.
    if can_reach_peers:
        available.extend(
            session_tools(_installed_agent_names(global_configuration, working_directory))
        )
        # Remote agents are a different bargain, so they are separate verbs and appear only when registered.
        if global_configuration.remote_agents.agents:
            available.extend(remote_agent_tools())
    # The caller's tools last, so a name collision resolves to ours rather than replacing a built-in.
    known = {tool.name for tool in available}
    available.extend(tool for tool in extra_tools if tool.name not in known)
    return available


def _as_profile(sandbox: Any):
    """Whatever a caller called a sandbox, as the :class:`Profile` the runtime works with."""
    from langmesh.base.confinement import Profile

    if sandbox is None:
        # The configured default, not `Profile()`: an empty writable set means "may write nowhere".
        from langmesh.base.configuration import SandboxConfiguration

        return SandboxConfiguration().to_profile()
    if isinstance(sandbox, Profile):
        return sandbox
    if isinstance(sandbox, dict):
        return Profile.from_dict(sandbox)
    to_profile = getattr(sandbox, "to_profile", None)
    if callable(to_profile):
        return to_profile()
    raise TypeError(
        f"sandbox must be a confinement Profile, a SandboxConfiguration, or the dict form of "
        f"either — got {type(sandbox).__name__}."
    )


def _build_tool_context(
    global_configuration: Configuration,
    *,
    sandbox,
    workspace: str,
    session_id: str = "",
    session_access: Any = None,
    conversation_snapshot: Optional[Callable[[], list[dict[str, Any]]]] = None,
    mcp_manager: Any = None,
) -> ToolContext:
    """The session-shaped state this runtime's tools read, derived from configuration rather than installed."""
    # The session's own tools, and the one widening that goes with them: it cannot install where it may not write.
    toolbox = toolbox_for(session_id, enabled=global_configuration.toolbox.enabled)
    if toolbox is not None:
        toolbox.prepare()
        from langmesh.base import confinement as _confinement

        sandbox = sandbox.with_grant(
            _confinement.approved(
                _confinement.AccessRequest(mutates=True, writes=(str(toolbox.root),)),
                by=_confinement.APPROVED_BY_RULE,
                purpose="the session's own toolbox directory",
            ),
            workspace=workspace,
        )

    exa_client = None
    exa_key = global_configuration.exa.effective_api_key
    if exa_key:
        from exa_py import Exa

        exa_client = Exa(api_key=exa_key)

    firecrawl_client = None
    firecrawl_key = global_configuration.firecrawl.effective_api_key
    if firecrawl_key:
        from firecrawl import AsyncFirecrawl

        api_url = global_configuration.firecrawl.effective_api_url
        firecrawl_client = (
            AsyncFirecrawl(api_key=firecrawl_key, api_url=api_url) if api_url
            else AsyncFirecrawl(api_key=firecrawl_key)
        )

    return ToolContext(
        sandbox=sandbox,
        workspace=workspace,
        exa_client=exa_client,
        mcp_manager=mcp_manager,
        firecrawl_client=firecrawl_client,
        jina_api_key=global_configuration.jina.effective_api_key,
        proxy_url=global_configuration.web_fetch.effective_proxy_url,
        fetch_timeout_seconds=global_configuration.web_fetch.timeout_seconds,
        download_timeout_seconds=global_configuration.web_fetch.download_timeout_seconds,
        minimum_useful_characters=global_configuration.web_fetch.minimum_useful_characters,
        session_access=session_access,
        conversation_snapshot=conversation_snapshot,
        session_id=session_id,
        toolbox=toolbox,
    )


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

    # What an update may say. A key outside this set is reported rather than silently matching nothing.
    UPDATE_KEYS = frozenset({"task_id", "status"})
    STATUSES = ("pending", "in_progress", "completed", "blocked")

    def update_tasks(self, updates: list[dict]) -> tuple[list[str], list[str]]:
        """Apply each update, returning the ids that changed and a complaint for each that did not."""
        updated_ids: list[str] = []
        complaints: list[str] = []
        known = {task.identifier for task in self._tasks}
        for update in updates:
            unknown = sorted(set(update) - self.UPDATE_KEYS)
            if unknown:
                complaints.append(
                    f"{', '.join(unknown)} is not part of an update; use {', '.join(sorted(self.UPDATE_KEYS))}."
                )
            task_id = update.get("task_id", "")
            status = update.get("status", "")
            if status not in self.STATUSES:
                complaints.append(f"{status!r} is not a status; use one of {', '.join(self.STATUSES)}.")
                continue
            if task_id not in known:
                complaints.append(f"There is no task {task_id!r}. Current ids: {', '.join(sorted(known)) or 'none'}.")
                continue
            for task in self._tasks:
                if task.identifier == task_id:
                    task.status = status
                    updated_ids.append(task_id)
                    break
        if updated_ids:
            self._recalculate_statuses()
        return updated_ids, complaints

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
        return compact([task.model_dump() for task in self._tasks])

    def to_dict_list(self) -> list[dict]:
        return [task.model_dump() for task in self._tasks]

    def snapshot(self) -> dict:
        """The manager's durable state, so a rebuilt runtime restores the tasks and keeps minting fresh ids."""
        return {"tasks": self.to_dict_list(), "next_identifier": self._next_identifier}

    def restore(self, snapshot: dict) -> None:
        """Rehydrate from :meth:`snapshot`, tolerating a missing or partial one by staying empty."""
        self._tasks = [TaskItem.model_validate(task) for task in snapshot.get("tasks", [])]
        self._next_identifier = int(snapshot.get("next_identifier", len(self._tasks) + 1))


class AgentRuntime(_DispatchesTools, _DecidesPermissions, _CompactsContext, _RunsTurns):
    # A turn runs until the model is done or the user interrupts: no ceiling and no stuck-detector.

    # Tool name to handler. `_execute_tool` resolves the shared preamble once, then dispatches here.
    _TOOL_HANDLERS = {
        "bash": "_tool_bash",
        "fetch_url": "_tool_fetch_url",
        "download_file": "_tool_download_file",
        "load_skill": "_tool_load_skill",
        "wait_for": "_tool_wait_for",
        "ask_user": "_tool_ask_user",
        "call_mcp_tool": "_tool_call_mcp_tool",
        "list_mcp_tools": "_tool_mcp_query",
        "list_mcp_resources": "_tool_mcp_query",
        "read_mcp_resource": "_tool_mcp_query",
        "set_tasks": "_tool_set_tasks",
        "update_tasks": "_tool_update_tasks",
        "update_goal": "_tool_update_goal",
        "search_web": "_tool_search_web",
        "read_turn": "_tool_read_turn",
        "control_screen": "_tool_control_screen",
        "create_session": "_tool_session",
        "message_session": "_tool_session",
        "read_session": "_tool_session",
        "list_sessions": "_tool_session",
        "list_remote_agents": "_tool_session",
        "message_remote_agent": "_tool_session",
    }

    def __init__(
        self,
        agent_configuration: AgentConfiguration,
        global_configuration: Configuration,
        session_id: str = "",
        conversation: Optional[list] = None,
        working_directory: str = "",
        project_directory: str = "",
        permission_mode: str = "",
        file_lease_manager: FileLeaseManager | None = None,
        locations: list[dict] | None = None,
        parent_session: str = "",
        sandbox=None,
        session_access: Any = None,
        mcp_manager: Any = None,
        model: Any = None,
        jobs: Any = None,
        observer: Any = None,
        approvals: Any = None,
        catalogue: Any = None,
        transcript: Any = None,
        turn_store: Any = None,
        tools: Sequence[BaseTool] = (),
        supplied_tool_gate: str = "ask",
        permissions: Any = None,
        hooks: Sequence[Any] = (),
        pipeline: Sequence[Any] = (),
        compaction: Any = None,
    ):
        from langmesh.runtime.hooks import HookRunner
        from langmesh.runtime.pipeline import ToolPipeline

        # The three seams around a turn, each defaulting to what the harness already did.
        self._hooks = HookRunner(hooks)
        self._pipeline = ToolPipeline(pipeline)
        self._compaction = compaction
        self._session_id = session_id
        # The session that created this one, empty when a person did. Reporting back needs its id.
        self._parent_session = parent_session
        # What every child is confined to, held rather than re-derived so a config edit cannot widen a live session.
        from langmesh.base.confinement import Grant

        # Normalised once, because callers hand this three different shapes.
        self._sandbox = _as_profile(sandbox)
        self._agent_configuration = agent_configuration
        self._global_configuration = global_configuration
        self._working_directory = working_directory or str(Path.home())
        self._project_directory = project_directory or self._working_directory
        # The locations the agent may address, with a local one synthesized when none were supplied.
        self._locations: dict[str, ResolvedLocation] = {}
        self._locations_by_name: dict[str, ResolvedLocation] = {}
        self._build_locations(
            locations, permission_mode_default=agent_configuration.permission_policy
        )

        effective_model = agent_configuration.model_identifier
        # Only a runtime that must build a client needs to be told which one.
        if not effective_model and model is None:
            raise ValueError(
                f"Agent '{agent_configuration.identifier}' names no model. Set `provider` and "
                "`model` in its profile, pass `model_identifier=\"provider/model\"` to "
                "`langmesh.Session`, or hand the runtime a `model=` of your own."
            )
        # A profile pinned to an unkeyed provider fails on its first call, which is the honest outcome.
        self._effective_model_identifier = effective_model

        # A caller's own model wins, since accepting `BaseChatModel` is the whole of the model seam.
        self._llm = model if model is not None else build_chat_model(
            effective_model, global_configuration, agent_configuration, self._working_directory,
            session_id,
        )

        self._file_lease_manager = file_lease_manager
        # The caller's tools alongside ours. `BaseTool` is adopted rather than wrapped.
        self._extra_tools = {tool.name: tool for tool in tools}
        # What a caller's tool is gated at: asking by default, so adding one cannot silently widen a session.
        self._supplied_tool_gate = supplied_tool_gate
        # Resolved before the tools, because which tools exist depends on it.
        self._permission_mode: PermissionMode = PermissionMode.more_restrictive(
            permission_mode, agent_configuration.permission_policy
        )
        self._tools = _build_tools(
            agent_configuration, global_configuration, self._working_directory,
            can_reach_peers=session_access is not None, extra_tools=tools,
            permission_mode=self._permission_mode,
        )
        # Tools are bound natively, so the provider sees each real schema and can emit several calls at once.
        self._tool_schemas: dict[str, Any] = {tool.name: tool.args_schema for tool in self._tools}
        self._bound_model = self._llm.bind_tools(self._tools)
        # The evaluator gates against the same narrowed allow-list the tool set was built from.
        self._permissions = permissions if permissions is not None else PermissionEvaluator(
            agent_configuration.model_copy(update={
                "tools_enabled": sorted(
                    _live_allow_list(agent_configuration.tools_enabled, {tool.name for tool in self._tools})
                ),
            })
        )
        self._background = BackgroundJobs(
            session_id=session_id,
            agent_name=agent_configuration.identifier,
            store=jobs,
        )
        # Where the audit trail goes, and who answers a gate. Both absent by default.
        self._observer = observer
        self._approvals = approvals
        self._transcript = transcript
        # Where a fold's entries are appended. Absent in a library run, which keeps no ledger.
        self._turn_store = turn_store
        # When the turn now running began, for the transcript entry it will produce.
        self._turn_started_at = None
        # The conversation and the prompt this runtime runs with.

        self._conversation: list = conversation if conversation is not None else []
        self._system_prompt = agent_configuration.system_prompt
        # Files read this session, by location and path with their hash, so a stale edit is rejected.
        self._abort_event = asyncio.Event()
        # Running token totals, summed from the usage each model call reports.
        self._token_usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cache_read_tokens": 0,
            # What a cache could have returned, since the read alone has no honest denominator.
            "reachable_tokens": 0,
            "reasoning_tokens": 0,
            "model_calls": 0,
        }

        # Where the prompt's material comes from, supplied rather than found by walking hardcoded paths.
        if catalogue is None:
            from langmesh.base.catalogue import machine_catalogue

            catalogue = machine_catalogue(global_configuration, self._project_directory)
        self._catalogue = catalogue
        self._prompt_loader = _CataloguePrompts(catalogue)
        self._cached_system_prompt: str | None = None
        self._task_manager = TaskManager()
        # The reviewer behind `automatic`, built on first use and kept for the session's life.
        self._reviewer_llm: Optional[BaseChatModel] = None
        self._goal: Optional[Goal] = None
        # Called when the goal changes, so the layer above can tell the daemon and the interface.
        self._on_goal_change: Optional[Callable[[Optional[Goal]], None]] = None
        # Set when the goal or tasks change, so durable state is written on mutation rather than every checkpoint.
        self._session_dirty = False
        self._execution_history: list[dict] = []
        # The permission policy as one value, clamped at creation against the parent and the card's ceiling.
        self._a2a_turn_id: str = ""
        # Reads another task by id from the shared store, so context-aware agents can coordinate.
        self._turn_reader: Optional[Callable] = None
        self._steering_messages: asyncio.Queue[str] = asyncio.Queue()
        self._steering_available = asyncio.Event()
        self._active_tool_tasks: dict[str, asyncio.Task] = {}
        # The latest call's occupancy and the model's window, so compaction can fire before an overflow.
        self._latest_context_tokens: int = 0
        self._context_window: int = 0
        # What the module-level tools read at call time, built from this runtime's own configuration and conversation.
        self._tool_context = _build_tool_context(
            global_configuration,
            sandbox=self._sandbox,
            workspace=self._working_directory,
            session_id=self._session_id,
            session_access=session_access,
            conversation_snapshot=self._peer_conversation_snapshot,
            mcp_manager=mcp_manager,
        )
        # What was approved beyond the configured profile, held for the session so one grant is not re-asked.
        self._access_grants: list[Grant] = []
        # The files the person attached: like a grant, but answering what they handed over rather than what was asked.
        self._attached_files: list[str] = []

    def note_attachments(self, paths: Sequence[str]) -> None:
        """Record attached files so a tool may read them where they live. Additive across the conversation."""
        for path in paths:
            if path and path not in self._attached_files:
                self._attached_files.append(path)

    def set_locations(self, locations: list[dict] | None) -> None:
        """Adopt the workspace's environments as they are now, so one added later reaches an existing session."""
        carried = {uri: resolved.executor for uri, resolved in self._locations.items()}
        self._locations = {}
        self._locations_by_name = {}
        self._build_locations(
            locations,
            permission_mode_default=self._agent_configuration.permission_policy,
            executors=carried,
        )
        # The system prompt is left alone: the environments are stated in the turn context, rebuilt every turn.

    def _build_locations(
        self,
        locations: list[dict] | None,
        *,
        permission_mode_default: PermissionMode,
        executors: dict[str, Any] | None = None,
    ) -> None:
        """The resolved-location map, each entry carrying an executor and its effective policy."""
        entries = locations or []
        if not entries:
            # None supplied: synthesize one local location, so the single-location default still applies.
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
                executor=(executors or {}).get(uri) or executor_for(address),
                permission_mode=PermissionMode.coerce(entry.get("permission_mode"), permission_mode_default),
            )
            self._locations[uri] = resolved
            self._locations_by_name[resolved.name] = resolved

    def _resolve_location(self, location_value: str | None) -> ResolvedLocation:
        """Resolve a call's ``location`` to its executor and policy, defaulting to the local filesystem."""
        if not location_value:
            if len(self._locations) == 1:
                return next(iter(self._locations.values()))
            # Default to local, so an omission is never executed on a remote host.
            local = next((location for location in self._locations.values() if location.kind == "local"), None)
            if local is not None:
                return local
            # Every location is remote, so require an explicit choice rather than picking one.
            names = ", ".join(sorted(self._locations_by_name)) or "(none configured)"
            raise ToolLocationError(
                f"This workspace has only remote locations and no local default — specify `location` (one of: {names})."
            )
        if location_value in self._locations:
            return self._locations[location_value]
        if location_value in self._locations_by_name:
            return self._locations_by_name[location_value]
        names = ", ".join(sorted(self._locations_by_name)) or "(none configured)"
        raise ToolLocationError(f"Unknown location {location_value!r}. Available: {names}.")

    def _call_policy(self, location: ResolvedLocation | None) -> CallExecutionPolicy:
        """One call's execution policy, as a value, so concurrent calls to different locations cannot cross."""
        mode = (
            self._permission_mode if location is None
            else PermissionMode.more_restrictive(self._permission_mode, location.permission_mode)
        )
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

    def _peer_conversation_snapshot(self) -> list[dict[str, Any]]:
        """Serialize the usable conversation prefix inherited by a newly created peer."""
        inherited_messages = self._conversation
        if inherited_messages and isinstance(inherited_messages[-1], AIMessage) and inherited_messages[-1].tool_calls:
            inherited_messages = inherited_messages[:-1]
        return messages_to_dict(inherited_messages)

    @property
    def background_jobs(self) -> BackgroundJobs:
        """This runtime's background-job runner, which the executor's resume pump reads."""
        return self._background

    def has_pending_jobs(self) -> bool:
        """Whether any background job is still in flight, without reaching into the runner's internals."""
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
        """Append a restored background result, so a rebuilt runtime replays it exactly like a live completion."""
        capped_result = _cap_model_result_payload(result, code=f"{kind}_result_truncated")
        metadata = _tool_timing_metadata(
            tool_name=kind,
            tool_call_identifier=tool_call_identifier,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            duration_milliseconds=0,
            background_job_id=identifier,
        )
        status, code = _model_result_status(capped_result, ok=True, backgrounded=False)
        self._conversation.append(self._reminder_message(
            _model_visible_tool_result(
                capped_result, metadata, status, code, kind="background_result",
            ),
        ))

    @property
    def token_usage(self) -> dict[str, int]:
        return dict(self._token_usage)

    def _accumulate_usage(self, response: AIMessage) -> TurnEvent | None:
        """Fold one call's usage into the session total and answer a USAGE event, or ``None`` when none was reported."""
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
        # What the adapter worked out about this request's prefix, read before the totals below.
        cache_trace = response.additional_kwargs.get("cache_trace") or {}
        # What could have been served, which is never less than what was, and zero on a session's first call.
        reachable = max(int(cache_trace.get("reachable_tokens", 0) or 0), cache_read)
        self._token_usage["input_tokens"] += input_tokens
        self._token_usage["output_tokens"] += output_tokens
        self._token_usage["total_tokens"] += total_tokens
        self._token_usage["cache_read_tokens"] += cache_read
        self._token_usage["reachable_tokens"] += reachable
        self._token_usage["reasoning_tokens"] += reasoning
        self._token_usage["model_calls"] += 1
        # The latest call's input is the whole prompt, so it says how full the context is.
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
            prefix_intact=bool(cache_trace.get("prefix_intact", False)),
            reachable_tokens=reachable,
            segments=int(cache_trace.get("segments", 0) or 0),
            shared_segments=int(cache_trace.get("shared_segments", 0) or 0),
            divergence=cache_trace.get("divergence"),
        )

    @property
    def agent_name(self) -> str:
        return self._agent_configuration.identifier

    @property
    def inline_image_bytes(self) -> int:
        """The ceiling on an image inlined into this conversation, as the person configured it."""
        return self._global_configuration.attachments.inline_image_bytes

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
    def writes_anywhere(self) -> bool:
        """Whether this session's confinement permits writing at all, which the operating system holds it to."""
        return bool(self._sandbox.filesystem.writable)

    def abort(self) -> None:
        # Stop tears down the live turn only: detached work and peer sessions have their own lifecycles.
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


    def background_snapshots(self) -> list[dict[str, Any]]:
        return self._background.active_snapshots()

    def send_tool_to_background(self, tool_call_identifier: str) -> bool:
        """Push a blocking command to the background on the user's behalf, as if the model had done it."""
        return self._background.request_background(tool_call_identifier)

    def enqueue_steering(self, message: str, message_id: str = "", peer_sender: str = "") -> bool:
        text = message.strip()
        if not text:
            return False
        self._steering_messages.put_nowait((text, message_id, peer_sender))
        self._steering_available.set()
        return True

    def discard_pending_steering(self) -> None:
        """Drop steering accepted too late to be honoured, since the client re-delivers it as a fresh turn."""
        while not self._steering_messages.empty():
            self._steering_messages.get_nowait()
        self._steering_available.clear()

    def _has_queued_steering(self) -> bool:
        return not self._steering_messages.empty()


    @property
    def configured_permission_mode(self) -> Optional[PermissionMode]:
        """The ceiling the agent's card declares, or ``None`` where it declares none — which is most cards."""
        return self._agent_configuration.permission_policy

    def set_permission_mode(self, mode: PermissionMode) -> PermissionMode:
        """Change the policy mid-life and answer with what took, reaching the very next tool call."""
        resolved = PermissionMode.more_restrictive(mode, self._agent_configuration.permission_policy)
        if resolved is self._permission_mode:
            return resolved
        self._permission_mode = resolved
        return resolved

    def set_a2a_turn_id(self, turn_id: str) -> None:
        """Record the current turn's task id, so work raised during it can name the turn it belongs to."""
        self._a2a_turn_id = turn_id

    def set_turn_reader(self, task_reader: Callable) -> None:
        """Install the reader `read_turn` uses to fetch related turns from the store."""
        self._turn_reader = task_reader

    def session_snapshot(self) -> dict:
        """The durable non-conversation state — the goal and the tasks — persisted beside the checkpoint."""
        return {
            "goal": self._goal.model_dump() if self._goal is not None else None,
            "tasks": self._task_manager.snapshot(),
        }

    def restore_session(self, snapshot: dict) -> None:
        """Rehydrate goal and tasks. A goal that does not validate is dropped rather than guessed at."""
        stored = snapshot.get("goal")
        goal = None
        if isinstance(stored, dict) and str(stored.get("text", "")).strip():
            try:
                goal = Goal.model_validate(stored)
            except ValidationError:
                logger.warning("discarding a stored goal that no longer validates")
        self._goal = goal
        self._task_manager.restore(snapshot.get("tasks", {}) or {})

    # The goal, and the four things anyone outside this class does with it.

    @property
    def goal(self) -> Optional[Goal]:
        """The session's goal, or ``None`` when it has none."""
        return self._goal

    def set_goal_listener(self, listener: Optional[Callable[[Optional[Goal]], None]]) -> None:
        """Install the callback that hears every goal change, which is how the interface learns of one."""
        self._on_goal_change = listener

    def write_goal(self, goal: Optional[Goal]) -> None:
        """Set, replace or drop the goal, and announce it. The single writer, so no path changes it silently."""
        self._goal = goal
        self._session_dirty = True
        if self._on_goal_change is not None:
            self._on_goal_change(goal)

    def note_goal_continuation(self) -> None:
        """Count one harness-opened turn against the goal's allowance."""
        if self._goal is None:
            return
        self.write_goal(self._goal.model_copy(update={"continuations": self._goal.continuations + 1}))

    def restore_goal_allowance(self) -> None:
        """A person spoke, so the allowance restarts and a parked goal resumes. A blocked one is left alone."""
        goal = self._goal
        if goal is None or goal.status == Goal.BLOCKED:
            return
        if goal.continuations == 0 and goal.status == Goal.ACTIVE:
            return
        self.write_goal(goal.model_copy(update={"continuations": 0, "status": Goal.ACTIVE}))

    def park_goal(self) -> None:
        """Stop working the goal until a person speaks. The goal is kept: it is still what the session is for."""
        if self._goal is None or not self._goal.is_open:
            return
        self.write_goal(self._goal.model_copy(update={"status": Goal.PARKED}))

    def dirty_session_snapshot(self) -> Optional[dict]:
        """The snapshot if it changed since the last persist. Peeks only: the flag clears after the write commits."""
        return self.session_snapshot() if self._session_dirty else None

    def clear_session_dirty(self) -> None:
        """Mark the durable state persisted, only after the atomic write succeeds."""
        self._session_dirty = False

    def _record_event(self, event_type: str, data: dict) -> None:
        record = {"type": event_type, "timestamp": _utc_timestamp(datetime.now(timezone.utc)), **data}
        self._execution_history.append(record)
        self._observe(event_type, data)

    def _record_message(self, role: str, content: str, tool_call_id: str = "") -> None:
        self._observe("message", {"role": role, "content": content, "tool_call_id": tool_call_id})

    def _observe(self, kind: str, data: dict) -> None:
        """Hand one observation to the caller's observer. One that raises must not take the turn with it."""
        if self._observer is None:
            return
        observation = Observation(
            session_id=self._session_id,
            kind=kind,
            at=datetime.now(timezone.utc),
            data=data,
        )
        try:
            pending = self._observer.observe(observation)
        except Exception:  # noqa: BLE001 — an audit sink must never fail a turn
            logger.debug("the observer raised on %s", kind, exc_info=True)
            return
        if pending is None:
            return
        try:
            asyncio.get_running_loop().create_task(_drain_observation(pending))
        except RuntimeError:
            # No loop: an observation outside a turn, with nothing to schedule it on.
            logger.debug("dropped an awaitable observation with no running loop")

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
                background_job_id=completion.identifier,
            )
            # Append-only: the placeholder stays and the result lands as a new user-role reminder, keeping the prefix.
            background_status, background_code = _model_result_status(
                capped_result, ok=True, backgrounded=False,
            )
            self._conversation.append(self._reminder_message(
                _model_visible_tool_result(
                    capped_result, background_metadata, background_status, background_code,
                    kind="background_result",
                ),
            ))
            events.append(ToolResult(id=completion.tool_call_identifier,
                name=completion.kind,
                result=_maybe_json(capped_result),
                status=background_status,
                job_id=completion.identifier,
            ))
            completion_event_data: dict[str, Any] = {"job_id": completion.identifier}
            if background_include_result(completion.kind):
                completion_event_data["result"] = capped_result
            self._record_event(background_completion_event(completion.kind), completion_event_data)
        return events

    def _model_supports_vision(self) -> bool:
        """Whether the model advertises image input. An unknown model is assumed capable, as elsewhere."""
        model = find_model(self._effective_model_identifier)
        return True if model is None else model.vision

    def get_execution_history(self) -> list[dict]:
        return self._execution_history
