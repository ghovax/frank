from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from langchain_core.messages import (
    AIMessage,
)
from langchain_core.tools import BaseTool
from pydantic import BaseModel, SecretStr


from frank.base.configuration import (
    AgentConfiguration,
    Configuration,
    PermissionEvaluator,
)
from langchain_core.language_models.chat_models import BaseChatModel
from frank.runtime.models.litellm import ChatLiteLLMModel
from frank.runtime.models.codex import ChatCodexModel
from frank.runtime.models.cursor import ChatCursorModel
from frank.base.file_leases import FileLeaseManager
from frank.base.models import find_model, resolve_litellm
from frank.locations.resolver import LocationAddress, executor_for, location_uri_for
from frank.runtime.tools.registry import (
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
    read_file as read_file_tool,
    search_code as search_code_tool,
    edit_file as edit_file_tool,
    write_file as write_file_tool,
    fetch_url as fetch_url_tool,
    download_file as download_file_tool,
    control_screen as control_screen_tool,
    ask_user as ask_user_tool,
    load_skill as load_skill_tool,
    wait_for as wait_for_tool,
)
from frank.base.ports import Observation
from frank.runtime.tools.context import ToolContext
from frank.runtime.tools.sessions import remote_agent_tools, session_tools
from frank.runtime.background import (
    BackgroundJobs,
    background_completion_event,
    background_include_result,
)


from frank.base.permission_mode import PermissionMode

from frank.runtime.locations import CallExecutionPolicy, ResolvedLocation, ToolLocationError
from frank.runtime.turn_events import (
    ToolResult,
    TurnEvent,
    Usage,
)

from frank.runtime.tools.dispatch import (
    _DispatchesTools,
)

from frank.runtime.turn import (
    _RunsTurns,
)

from frank.runtime.permissions import (
    _DecidesPermissions,
)

from frank.runtime.compaction import (
    _CompactsContext,
)
from frank.base.serialization import compact
from frank.runtime.internals import (
    _cap_model_result_payload,
    _maybe_json,
    _model_result_status,
    _model_visible_tool_result,
    _tool_timing_metadata,
    _utc_timestamp,
)


logger = logging.getLogger(__name__)


class _CataloguePrompts:
    """A `PromptLoader`-shaped view of a catalogue.

    An adapter rather than a rewrite: `load(name, variables)` is the call every prompt site in
    the turn loop already makes, and the catalogue answers it. Keeping the shape means the
    template seam cost one class instead of a change at every render site.
    """

    def __init__(self, catalogue: Any) -> None:
        self._catalogue = catalogue

    def load(self, template_name: str, variables: dict[str, str]) -> str:
        return self._catalogue.prompt(template_name, variables)


async def _drain_observation(pending) -> None:
    """Await an observer's awaitable, swallowing whatever it does.

    Separate from `_observe` because a task's exception is only ever seen when the task is
    awaited, and nothing awaits this one — without the guard a failing async observer would
    surface as an "exception was never retrieved" warning at an unrelated moment."""
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
    """Build the chat model for a provider-qualified (``provider/model``) id.

    Almost every provider flows through one ``ChatLiteLLMModel`` (LiteLLM owns each
    provider's auth, base URL, request format, and reasoning normalization). The
    exceptions are the two experimental subscription providers, which are not LiteLLM
    routes at all: ``chatgpt`` uses ``ChatCodexModel`` against Codex's Responses
    endpoint, and ``cursor`` uses ``ChatCursorModel`` against Cursor's agent service.
    Both read their OAuth token from their own store rather than taking an api_key.

    ``working_directory`` is only consulted by the cursor provider, whose request context
    wants to know where the client believes it is running. It is required even so: a default
    would exist only to spare a caller from passing what it already has, and the caller that
    took the default would be the one silently telling Cursor it is running nowhere.

    ``session_id`` becomes the provider's prompt cache key, so that a conversation's growing
    prefix is looked for in one place rather than wherever a request happens to be routed. It
    does default, because a library caller building a bare model has no session and the only
    cost of omitting it is the cache miss it already had."""
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
        # No reasoning_effort: a Cursor model id carries its effort as part of the id, so
        # the user's model choice already said it and a second setting could only disagree.
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
    # The catalogue's window travels with the model, the same way it already does on the Codex
    # path. LiteLLM knows nothing about a gateway's models, so without this the window was zero
    # for every one of them — and a zero window is not a small window, it is no fill ring, no
    # scaled budget, and no over-context guard.
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
) -> list[BaseTool]:
    tools = _all_available_tools(
        agent_configuration, global_configuration, working_directory,
        can_reach_peers=can_reach_peers, extra_tools=extra_tools,
    )
    # The agent profile's allow-list narrows *our* tools. It cannot narrow the caller's: a
    # profile is a file describing which of the harness's capabilities an agent should have,
    # written long before this program existed, so filtering a tool the caller passed in
    # against it would mean a supplied tool silently vanishing for every agent that names an
    # explicit list.
    supplied = {tool.name for tool in extra_tools}
    allowed = _live_allow_list(
        agent_configuration.tools_enabled, {tool.name for tool in tools} - supplied,
    )
    # `disabled` applies to the harness's tools and to the caller's alike: a program that
    # supplies a tool and then switches it off for one agent means it, and the allow-list
    # exemption above is about a *profile* not knowing the tool exists, not about the caller.
    return [
        tool for tool in tools
        if agent_configuration.tools.is_enabled(tool.name)
        and (tool.name in supplied or not allowed or tool.name in allowed)
    ]


def _live_allow_list(configured: list[str], existing: set[str]) -> set[str]:
    """An agent's tool allow-list, narrowed to tools that exist.

    A profile may name a tool that has since been removed — every shipped profile listed the
    delegation tool the peer-session tools replaced. Since an allow-list denies everything it
    does not name, keeping a dead entry would leave a profile that permits nothing at all, and a
    non-destructive seed means the stale copy in a user's home directory outlives the fix.
    Dropping the names that match nothing turns such a list back into what it plainly meant."""
    live = {name for name in configured if name in existing}
    return live


def _installed_agent_names(global_configuration: Configuration, working_directory: str) -> list[str]:
    """The agent profiles a peer could be created with, here.

    Read at build time so `create_session` can enumerate them in its schema rather than take a
    free string: a name that does not exist becomes unrepresentable instead of being refused
    after the call. Project-local profiles are resolved from the working directory, so a
    session sees what its own project installs."""
    from frank.base.configuration import list_agents

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
) -> list[BaseTool]:
    available = [
        bash_tool,
        read_file_tool,
        search_code_tool,
        edit_file_tool,
        write_file_tool,
        fetch_url_tool,
        download_file_tool,
        load_skill_tool,
        wait_for_tool,
        search_web_tool,
        set_tasks_tool,
        update_tasks_tool,
        update_goal_tool,
        read_turn_tool,
        # A session can always ask the user directly: the question parks the turn as a
        # human-in-the-loop gate and resumes on the answer.
        ask_user_tool,
    ]
    # Searching and controlling the live screen (the browser and native apps) drives the whole
    # machine, so it is opt-in: added only when the user has enabled it in Settings (which also
    # gates the Accessibility grant flow).
    if global_configuration.computer_control.enabled:
        available.append(control_screen_tool)
    if global_configuration.mcp.enabled_servers():
        available.extend([
            list_mcp_tools_tool,
            call_mcp_tool_tool,
            list_mcp_resources_tool,
            read_mcp_resource_tool,
        ])
    # Peer sessions: the one composition path. Offered only when there is a profile to run
    # *and* a control plane to reach — a `create_session` with an empty enumeration, or with no
    # way to address what it creates, is a tool that cannot succeed, and offering one costs
    # context and invites an attempt. A library session has no control plane, so it composes by
    # calling the harness again rather than by creating a peer.
    if can_reach_peers:
        available.extend(
            session_tools(_installed_agent_names(global_configuration, working_directory))
        )
        # Agents on other hosts are a different bargain — someone else's machine, someone
        # else's cost, no access to this filesystem — so they are separate verbs, and they
        # appear only when the user has actually registered one.
        if global_configuration.remote_agents.agents:
            available.extend(remote_agent_tools())
    # The caller's own tools, last, so a name collision resolves to ours rather than silently
    # replacing a built-in — a tool called `bash` that is not this harness's `bash` would be a
    # confinement surprise, not an extension point.
    known = {tool.name for tool in available}
    available.extend(tool for tool in extra_tools if tool.name not in known)
    return available


def _build_tool_context(
    global_configuration: Configuration,
    *,
    sandbox,
    workspace: str,
    session_access: Any = None,
    mcp_manager: Any = None,
) -> ToolContext:
    """The session-shaped state this runtime's tools read.

    The capability clients are derived from configuration rather than installed into the
    runtime, because that is what they are: a key in the configuration file is the whole of
    what makes web search or a Firecrawl fetch available. Reading them from a global meant a
    settings change could be half-applied — the configuration updated and the client not, or
    the reverse. The two arguments that are *not* configuration are the two the runtime cannot
    derive: which session this is, and the MCP connections somebody else owns the lifetime of.
    """
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
        session_access=session_access,
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

    #: What an update may say. A key outside this set is reported rather than ignored: the tool
    #: documented `turn_id` while this read `task_id`, so every well-formed-looking update matched
    #: nothing and came back as "No matching tasks found" — a message that describes the task list
    #: rather than the mistake, and gives a model no way to find the difference.
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
        """The manager's full durable state — the task list plus the id counter — so a
        rebuilt runtime restores identical tasks and keeps minting non-colliding ids."""
        return {"tasks": self.to_dict_list(), "next_identifier": self._next_identifier}

    def restore(self, snapshot: dict) -> None:
        """Rehydrate from :meth:`snapshot`. Tolerates a missing or partial snapshot (a
        session that never set tasks) by leaving the manager empty."""
        self._tasks = [TaskItem.model_validate(task) for task in snapshot.get("tasks", [])]
        self._next_identifier = int(snapshot.get("next_identifier", len(self._tasks) + 1))


class AgentRuntime(_DispatchesTools, _DecidesPermissions, _CompactsContext, _RunsTurns):
    # A turn runs until the model is done or the user interrupts it — there is no tool-call
    # ceiling and no heuristic stuck-detector. The model owns progress: it ends its own turn
    # when finished, uses ``wait_for`` to poll rather than spinning, and re-reads a tool's
    # ``output_file`` to see whether a repeated action changed anything. Context compaction is
    # Observational Memory (Observer/Reflector); its thresholds and on/off switch live in
    # Configuration.compaction.

    # Tool-name -> handler method. ``_execute_tool`` resolves permission, location, and
    # policy once (the shared preamble), then dispatches the call to its handler here.
    # Grouped tools (edit/write, the peer-session verbs, the MCP queries, computer/browser) share
    # one handler; an unmapped name is the "unknown tool" error.
    _TOOL_HANDLERS = {
        "bash": "_tool_bash",
        "read_file": "_tool_read_file",
        "search_code": "_tool_search_code",
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
        "end_session": "_tool_session",
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
        tools: Sequence[BaseTool] = (),
        tool_risk: str = "medium",
        permissions: Any = None,
        hooks: Sequence[Any] = (),
        pipeline: Sequence[Any] = (),
        compaction: Any = None,
    ):
        from frank.runtime.hooks import HookRunner
        from frank.runtime.pipeline import ToolPipeline

        # The three seams around a turn. Each defaults to what the harness has always done:
        # no hooks, no middleware, and the Observer/Reflector compaction built from
        # configuration. Passing none of them changes nothing.
        self._hooks = HookRunner(hooks)
        self._pipeline = ToolPipeline(pipeline)
        self._compaction = compaction
        self._session_id = session_id
        # The session that created this one, empty when a person did. Reaches the model in the
        # turn context, because reporting back is sending it a message and that needs an id.
        self._parent_session = parent_session
        # What every child this runtime spawns is confined to. Resolved by the daemon at session
        # creation and clamped there; held rather than re-derived, so a configuration edit cannot
        # widen a session that is already running.
        from frank.base.confinement import Profile

        self._sandbox = sandbox if sandbox is not None else Profile()
        self._agent_configuration = agent_configuration
        self._global_configuration = global_configuration
        self._working_directory = working_directory or str(Path.home())
        self._project_directory = project_directory or self._working_directory
        # The workspace's locations the agent may address per tool call (keyed by URI, and
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
                f"Agent '{agent_configuration.identifier}' names no model. Set `provider` and "
                "`model` in its profile, pass `model_identifier=\"provider/model\"` to "
                "`frank.Session`, or hand the runtime a `model=` of your own."
            )
        # A profile pinned to a provider the user never keyed fails on its first call, and
        # that is the honest outcome: there is nothing to fall back to. Borrowing another
        # profile's model would make one agent's behaviour depend on a second one it never
        # named — the coupling every agent is defined to be free of — and would quietly run
        # the work on a model nobody chose for it.
        self._effective_model_identifier = effective_model

        # A caller's own chat model wins over anything configuration would build. Every provider
        # this harness routes to implements LangChain's `BaseChatModel`, and so does every mock,
        # tracer and rate limiter in that ecosystem — so accepting one is the whole of the model
        # seam, with no interface of ours in the middle.
        self._llm = model if model is not None else build_chat_model(
            effective_model, global_configuration, agent_configuration, self._working_directory,
            session_id,
        )

        self._file_lease_manager = file_lease_manager
        # The caller's own tools, alongside the harness's. `BaseTool` is LangChain's, adopted
        # rather than wrapped, so anything already written for that ecosystem works unchanged.
        self._extra_tools = {tool.name: tool for tool in tools}
        # What a caller's tool is gated at. The permission engine classifies by tool *name* and
        # has never heard of this one, so there is no honest way to infer it; defaulting to a
        # mode that asks means adding a tool cannot silently widen what a session may do.
        self._tool_risk = tool_risk
        self._tools = _build_tools(
            agent_configuration, global_configuration, self._working_directory,
            can_reach_peers=session_access is not None, extra_tools=tools,
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
        self._bound_model = self._llm.bind_tools(self._tools)
        # The evaluator gates against the same narrowed allow-list the tool set was built
        # from, so a profile naming a tool that no longer exists cannot refuse everything.
        # A caller's own evaluator replaces the rule engine entirely. `Approvals` answers a
        # gate once the engine has decided there should be one; this decides whether there is
        # one at all, which is the difference between a policy and a decision.
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
        # Where the audit trail goes, and who answers a gate. Both absent by default, and both
        # absences are the behaviour that was already there: the observations were computed and
        # dropped, and a gate suspended and waited for a human.
        self._observer = observer
        self._approvals = approvals
        self._transcript = transcript
        # When the turn now running began, for the transcript entry it will produce.
        self._turn_started_at = None
        # Command patterns the user chose to "always allow" this session — matching
        # bash commands then skip the sandbox/approval prompts. Scoped to this
        # runtime (this context), populated on demand from an LLM-derived rule.

        self._conversation: list = conversation if conversation is not None else []
        self._system_prompt = agent_configuration.system_prompt
        # Files the model has read this session, keyed by (location uri, resolved
        # path) with the content hash from the last read — the uri disambiguates a
        # same-named path on two hosts. Mutating tools compare against this so
        # stale line numbers cannot overwrite externally changed content.
        self._read_files: dict[tuple[str, str], str] = {}
        self._abort_event = asyncio.Event()
        # Running token totals for the session, summed from the real usage each
        # model call reports (LiteLLM ``usage`` -> message ``usage_metadata``).
        self._token_usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cache_read_tokens": 0,
            # What a cache could have returned across the session — the tokens each call had
            # already sent last time. Kept beside the read because the read alone has no honest
            # denominator: measured against total input it looks like a miss even when every
            # cacheable token came back, since a token must be sent once before it can be cached.
            "reachable_tokens": 0,
            "reasoning_tokens": 0,
            "model_calls": 0,
        }

        # Where the prompt's material comes from: agent profiles, skills, memories, the
        # project's instructions, and the templates themselves. Supplied rather than derived,
        # because finding it means walking hardcoded paths — including, before this, two other
        # products' configuration files out of the user's home directory.
        if catalogue is None:
            from frank.base.catalogue import machine_catalogue

            catalogue = machine_catalogue(global_configuration, self._project_directory)
        self._catalogue = catalogue
        self._prompt_loader = _CataloguePrompts(catalogue)
        self._cached_system_prompt: str | None = None
        self._task_manager = TaskManager()
        self._active_goal: str = ""
        # Set when the goal or task list changes, so the executor persists the durable
        # session state only on mutation rather than on every checkpoint.
        self._session_dirty = False
        self._execution_history: list[dict] = []
        # The session's permission policy is ONE typed value, fixed for its whole life: the
        # mode is chosen at `create` (clamped against the parent's, and against the agent
        # card's own ceiling) and nothing can loosen it afterwards. The read_only/auto
        # booleans the call sites read are derived views of it, never separate state.
        # `more_restrictive` ignores absent inputs and falls back to the interactive default
        # with none, which is what a session with neither a requested mode nor a card ceiling
        # should get.
        self._permission_mode: PermissionMode = PermissionMode.more_restrictive(
            permission_mode, agent_configuration.permission_policy
        )
        self._a2a_turn_id: str = ""
        # Reads another A2A task (sibling/agent) by id from the shared store,
        # so context-aware agents can coordinate. Injected by the executor.
        self._turn_reader: Optional[Callable] = None
        self._steering_messages: asyncio.Queue[str] = asyncio.Queue()
        self._steering_available = asyncio.Event()
        self._active_tool_tasks: dict[str, asyncio.Task] = {}
        # The latest call's context occupancy (prompt + completion) and the model's
        # window, tracked from usage so auto-compaction can fire before the next call
        # would overflow. Zero until the first call reports usage.
        self._latest_context_tokens: int = 0
        self._context_window: int = 0
        # What the module-level tools read at call time. Built here, from this runtime's own
        # configuration and the two things only the layer above can supply — how this session
        # reaches its peers, and the MCP connections it owns the lifecycle of. It used to be
        # eight process globals installed by the worker at startup, which held only because a
        # worker serves one session; `dispatch` rewrote one of them mid-turn.
        self._tool_context = _build_tool_context(
            global_configuration,
            sandbox=self._sandbox,
            workspace=self._working_directory,
            session_access=session_access,
            mcp_manager=mcp_manager,
        )

    def set_locations(self, locations: list[dict] | None) -> None:
        """Adopt the workspace's environments as they are now.

        An environment added to a workspace used to reach only sessions created afterwards:
        the set was resolved at `session.create`, travelled in the assignment, and was never
        looked at again — so adding a folder to a workspace you were already talking in did
        nothing you could see, and the only way to get it was a new conversation.

        Rebuilt rather than mutated in place, because a location's name and its executor are
        derived from its address and both can change under an edit. An executor already built
        for an unchanged address is carried over, so re-reading the workspace does not throw
        away a remote's memoized home directory (and, with it, a round trip on the next call).
        A tool call in flight holds its own resolved location by value, so it is unaffected.
        """
        carried = {uri: resolved.executor for uri, resolved in self._locations.items()}
        self._locations = {}
        self._locations_by_name = {}
        self._build_locations(
            locations,
            permission_mode_default=self._agent_configuration.permission_policy,
            executors=carried,
        )
        # The system prompt is deliberately left alone. The environments and their policies are
        # stated to the model in the *turn context*, which is rebuilt every turn anyway, so the
        # next turn already names the current set. Dropping the cached prompt here used to be
        # required and is now merely expensive: it is the front of every request, so rebuilding
        # it discards the provider's prompt cache for the whole conversation — a full-price
        # re-read of everything, to restate something that was not in it.

    def _build_locations(
        self,
        locations: list[dict] | None,
        *,
        permission_mode_default: PermissionMode,
        executors: dict[str, Any] | None = None,
    ) -> None:
        """Build the resolved-location map from the workspace's location records. Each entry
        carries an executor (local subprocess or multiplexed SSH) and its effective policy."""
        entries = locations or []
        if not entries:
            # No workspace locations supplied — synthesize a single local location at the
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
                executor=(executors or {}).get(uri) or executor_for(address),
                permission_mode=PermissionMode.coerce(entry.get("permission_mode"), permission_mode_default),
            )
            self._locations[uri] = resolved
            self._locations_by_name[resolved.name] = resolved

    def _resolve_location(self, location_value: str | None) -> ResolvedLocation:
        """Resolve a tool call's ``location`` (a URI, or a location name) to its executor +
        policy. Omitted defaults to the workspace's local filesystem — so a call never has to
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
                f"This workspace has only remote locations and no local default — specify `location` (one of: {names})."
            )
        if location_value in self._locations:
            return self._locations[location_value]
        if location_value in self._locations_by_name:
            return self._locations_by_name[location_value]
        names = ", ".join(sorted(self._locations_by_name)) or "(none configured)"
        raise ToolLocationError(f"Unknown location {location_value!r}. Available: {names}.")

    def _call_policy(self, location: ResolvedLocation | None) -> CallExecutionPolicy:
        """The execution policy for one tool call. The session's own mode governs, except
        that a location carrying its own explicit mode governs calls against that location —
        and a read-only session is a floor no location can lift. Returned as a value and
        threaded through the call, never written to shared state, so concurrent calls to
        different locations cannot cross policies."""
        if location is None or location.permission_mode is PermissionMode.DEFAULT:
            mode = self._permission_mode
        elif self._permission_mode is PermissionMode.READ_ONLY:
            mode = PermissionMode.READ_ONLY
        else:
            mode = PermissionMode.more_restrictive(self._permission_mode, location.permission_mode)
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
    def background_jobs(self) -> BackgroundJobs:
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
        # What the adapter worked out about this request's prefix, read before the totals below
        # because one of them is now part of it.
        cache_trace = response.additional_kwargs.get("cache_trace") or {}
        # What could have been served, which is never less than what *was*.
        #
        # The estimate comes from comparing this request against the last one this process sent,
        # so it is zero whenever there is no last one — the first call of a session, and every
        # call after the session slept, because waking forks a fresh worker that has never sent
        # anything. The provider's cache does not forget across that boundary, so it happily
        # returned tokens against an estimate of zero, and the running totals came out at 106%.
        # The provider's own figure is the authority on what it had: an estimate below it is an
        # estimate that is wrong, and the two counts differ by a few percent anyway for having
        # been made with different tokenizers.
        reachable = max(int(cache_trace.get("reachable_tokens", 0) or 0), cache_read)
        self._token_usage["input_tokens"] += input_tokens
        self._token_usage["output_tokens"] += output_tokens
        self._token_usage["total_tokens"] += total_tokens
        self._token_usage["cache_read_tokens"] += cache_read
        self._token_usage["reachable_tokens"] += reachable
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
    def _self_classifies(self) -> bool:
        return self._permission_mode.is_self_classifying

    def abort(self) -> None:
        # Stop tears down only the live turn: signal the loop to end and kill every
        # foreground tool still running. Detached background work has its own lifecycle and
        # must not become collateral of steering the main flow — nor does a peer session,
        # which is a separate process ended with `frank kill`, not by stopping this turn.
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


    @property
    def configured_permission_mode(self) -> Optional[PermissionMode]:
        """The permission mode the agent's own card declares as its ceiling, or ``None`` where
        it declares none — which is most cards, and means they bound nothing."""
        return self._agent_configuration.permission_policy

    def set_permission_mode(self, mode: PermissionMode) -> PermissionMode:
        """Change the policy this runtime enforces, mid-life, and answer with what took.

        The one mutation of `_permission_mode` after construction, and it is safe for the same
        reason the field could be a constant before: every decision reads it at call time
        through `_call_policy`, so the next tool call in the turn already in flight is judged
        by the new mode rather than by the one the turn started under. That immediacy is the
        point — a person changing this is reacting to what the session is doing right now.

        Clamped against the agent card's ceiling exactly as the constructor clamps it, so this
        can only ever be as loose as the profile itself allows.

        The model is told about the change through the turn context, which is rebuilt on every
        turn, so the next one already describes the policy now in force. It used to be told
        through the system prompt, which meant this had to discard the cached prompt — and the
        prompt is the front of every request, so a person toggling the mode threw away the
        provider's cache for the entire conversation and paid to re-read all of it. Enforcement
        was never the thing that lagged: `_call_policy` reads this field at call time, so the
        very next tool call is judged by the new mode either way."""
        resolved = PermissionMode.more_restrictive(mode, self._agent_configuration.permission_policy)
        if resolved is self._permission_mode:
            return resolved
        self._permission_mode = resolved
        return resolved

    def set_a2a_turn_id(self, turn_id: str) -> None:
        """Record the A2A task id of the current turn, so work raised during it can name the
        turn it belongs to."""
        self._a2a_turn_id = turn_id

    def set_turn_reader(self, task_reader: Callable) -> None:
        """Install the reader `read_turn` uses to fetch related turns from the store."""
        self._turn_reader = task_reader

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
        """The interactive ("manual") permission policy: not auto-classifying and not
        read-only. Under it, a command the card does not explicitly allow is asked
        (escalated to the user) rather than run. Auto self-classifies and read-only
        hard-blocks mutations, so neither asks on an unmatched command."""
        return self._permission_mode.is_interactive

    def _record_event(self, event_type: str, data: dict) -> None:
        record = {"type": event_type, "timestamp": _utc_timestamp(datetime.now(timezone.utc)), **data}
        self._execution_history.append(record)
        self._observe(event_type, data)

    def _record_message(self, role: str, content: str, tool_call_id: str = "") -> None:
        self._observe("message", {"role": role, "content": content, "tool_call_id": tool_call_id})

    def _observe(self, kind: str, data: dict) -> None:
        """Hand one observation to the caller's observer, if there is one.

        An observer that raises must not take the turn with it: this is a reporting channel,
        and a turn that fails because its audit sink failed would be strictly worse than one
        that is not audited. An implementation may answer with an awaitable — the tolerance
        `MCPEventCallback` already uses here — which is scheduled rather than awaited, because
        every caller of this is synchronous and deep inside a turn."""
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
            # No loop: an observation recorded outside a turn. Nothing to schedule it on, and
            # blocking to run it would be worse than dropping it.
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
            # Append-only: the scheduled placeholder ToolMessage stays put and the
            # result lands as a *new* message (a user-role reminder — a system
            # role here would be hoisted into Anthropic's top-level system param and
            # bust the whole prefix; see _reminder_message). Rewriting the
            # placeholder in place would change the conversation mid-stream and
            # invalidate the provider's prompt cache from that point on — re-billing
            # the whole suffix. The placeholder already satisfies its tool_call, so
            # appending keeps the prefix monotonic (always cacheable) while the model
            # still sees the result. Same canonical envelope as an inline tool
            # result, wrapped so the model reads it as a background delivery.
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
        """Whether the agent's model advertises image input. Unknown models (a
        custom endpoint not in the catalog) are assumed vision-capable — the
        same permissive default the attachment-inlining path uses."""
        model = find_model(self._effective_model_identifier)
        return True if model is None else model.vision

    def get_execution_history(self) -> list[dict]:
        return self._execution_history
