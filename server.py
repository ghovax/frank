import asyncio
import json
import platform
import re
import shutil
import subprocess

import httpx
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import quote, urljoin

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy import Column, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sse_starlette.sse import EventSourceResponse
from watchfiles import awatch
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, messages_from_dict, messages_to_dict
from pydantic import BaseModel, Field, SecretStr

from a2a.server.apps.jsonrpc import A2AFastAPIApplication
from a2a.server.request_handlers import DefaultRequestHandler

from harness.core.a2a_executor import (
    AgentRegistry,
    HarnessAgentExecutor,
    agent_rpc_path,
    build_agent_card,
)
from harness.core.task_store import AppendOnlyTaskStore
import harness.core.configuration as _configuration
from harness.core.configuration import (
    GlobalConfiguration,
    PromptLoader,
    database_file_path,
    list_agent_route_names,
    list_agents,
    load_agent_configuration,
    save_api_keys,
)
from harness.core.composio_router import composio_mcp_servers
from harness.core.litellm_model import ChatLiteLLMModel
from harness.core.mcp_client import MCPClientManager
from harness.core.models import MODELS, available_models, find_model, resolve_litellm
from harness.core.providers import PROVIDERS
from harness.core.skills import load_skills, skills_for_agent
from harness.tools.tools import (
    cancel_all_background_tasks,
    set_exa_client,
    set_mcp_client_manager,
    _inject_widget_runtime,
)

# Load .env (gitignored) so API keys are available via the environment without
# being stored in the tracked configuration.yaml. Existing env vars win, so a
# direnv-provided environment is not overridden.
load_dotenv()

PUBLIC_BASE_URL = "http://localhost:8822"
AGENT_CARD_PATH = "/.well-known/agent-card.json"

Base = declarative_base()


class SessionRecord(Base):
    """A chat session — one A2A context. Tasks live in the A2A task store; this
    table only indexes sessions for the sidebar."""

    __tablename__ = "sessions"

    id = Column(String, primary_key=True)  # == A2A contextId
    agent = Column(String, nullable=False)
    working_directory = Column(Text, default="")
    title = Column(Text, default="")
    # Per-session model override (provider/model id); empty means use the global
    # default_model. Persisted so resuming a session keeps its chosen model.
    model = Column(Text, default="")
    created_at = Column(String, nullable=False)


class ProjectHistoryRecord(Base):
    """Recent working directories selected in the UI."""

    __tablename__ = "project_history"

    path = Column(Text, primary_key=True)
    name = Column(Text, default="")
    selected_at = Column(String, nullable=False)


class ModelHistoryRecord(Base):
    """Recently selected models (provider/model id + label), mirroring the project
    history so a user can quickly switch back to a model they used before."""

    __tablename__ = "model_history"

    model_id = Column(Text, primary_key=True)
    name = Column(Text, default="")
    provider = Column(Text, default="")
    selected_at = Column(String, nullable=False)


class ConversationRecord(Base):
    """The agent's dialogue history (LangChain messages) per A2A context, persisted
    so a session keeps its context across a server restart. The A2A task store holds
    the transcript the UI replays; this holds the model-facing message list the agent
    actually resumes from."""

    __tablename__ = "conversations"

    context_id = Column(String, primary_key=True)  # == A2A contextId
    messages = Column(Text, default="")  # JSON: langchain messages_to_dict
    updated_at = Column(String, nullable=False)


class Broadcaster:
    """A tiny in-process pub/sub. Subscribers receive every broadcast event,
    which is how live changes (e.g. edited agents) reach connected clients."""

    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait(event)


_global_configuration: Optional[GlobalConfiguration] = None
_session_factory: Optional[sessionmaker] = None
_async_engine = None
_task_store: Optional[AppendOnlyTaskStore] = None
_registry: Optional[AgentRegistry] = None
_mcp_manager: Optional[MCPClientManager] = None
# Composio Tool Router server(s), provisioned once at startup. Kept separate from
# the mcp.json-derived servers so the file watcher's live reload re-merges them
# instead of dropping Composio whenever mcp.json changes.
_composio_servers: dict[str, _configuration.MCPServerConfiguration] = {}
_executors: dict[str, HarnessAgentExecutor] = {}
_mounted_agents: set[str] = set()
_pending_permissions: dict[str, asyncio.Future] = {}
# Pending ask_user answers, keyed by request id (prefixed "q-<context_id>-").
# Mirrors _pending_permissions: the runtime awaits a future the UI resolves via
# the /chat/{context_id}/question endpoint.
_pending_questions: dict[str, asyncio.Future] = {}
# Dialogue history per A2A context, shared across every agent executor so that
# switching the active agent continues the same conversation (the persona is
# applied per-turn on top of this shared history).
_conversations: dict[str, list] = {}
# How many top-level turns are running per context. Drives the sidebar's
# "running" spinner; a count rather than a flag so overlapping turns are handled.
_running_contexts: dict[str, int] = {}


def _set_turn_state(context_id: str, running: bool) -> None:
    """Track active turns per context and broadcast on the empty/active edge so the
    sidebar reflects which conversations are currently running."""
    previous = _running_contexts.get(context_id, 0)
    updated = previous + 1 if running else max(0, previous - 1)
    if updated:
        _running_contexts[context_id] = updated
    else:
        _running_contexts.pop(context_id, None)
    if (previous == 0) != (updated == 0):
        _broadcaster.publish({"type": "sessions_changed"})


def _notify_permission_state(context_id: str) -> None:
    """A turn raised (or settled) a permission request — refresh the sidebar so it
    can swap the spinner for an attention marker on the waiting session."""
    _broadcaster.publish({"type": "sessions_changed"})

_broadcaster = Broadcaster()
# Keeps references to in-flight session-title generation tasks so they are not
# garbage-collected before completing.
_title_tasks: set[asyncio.Task] = set()


def _load_conversation(context_id: str) -> list:
    """Restore a context's persisted dialogue history (LangChain messages). Returns
    an empty list when there is nothing stored or the stored form can't be decoded —
    a resumed session then simply starts fresh rather than erroring."""
    if _session_factory is None:
        return []
    database_session = _session_factory()
    try:
        record = database_session.get(ConversationRecord, context_id)
        if record is None or not record.messages:
            return []
        return messages_from_dict(json.loads(record.messages))
    except Exception:
        return []
    finally:
        database_session.close()


def _save_conversation(context_id: str, messages: list) -> None:
    """Persist a context's dialogue history after a turn, so it survives a restart."""
    if _session_factory is None or not context_id:
        return
    database_session = _session_factory()
    try:
        serialized = json.dumps(messages_to_dict(messages))
        record = database_session.get(ConversationRecord, context_id)
        now = datetime.now(timezone.utc).isoformat()
        if record is None:
            database_session.add(ConversationRecord(context_id=context_id, messages=serialized, updated_at=now))
        else:
            record.messages = serialized
            record.updated_at = now
        database_session.commit()
    except Exception:
        database_session.rollback()
    finally:
        database_session.close()


def _session_model_for(context_id: str) -> str:
    """Read a context's persisted per-session model override (``""`` = global
    default). Used when building a runtime so a conversation runs on the model the
    user picked for it."""
    if _session_factory is None or not context_id:
        return ""
    database_session = _session_factory()
    try:
        record = database_session.get(SessionRecord, context_id)
        return (record.model or "") if record is not None else ""
    except Exception:
        return ""
    finally:
        database_session.close()


def _set_session_model(context_id: str, model_identifier: str) -> bool:
    """Persist a per-session model override and drop the cached runtime so the next
    turn rebuilds with the new model. Returns whether the session was found."""
    if _session_factory is None or not context_id:
        return False
    database_session = _session_factory()
    updated = False
    try:
        record = database_session.get(SessionRecord, context_id)
        if record is None:
            return False
        record.model = model_identifier or ""
        database_session.commit()
        updated = True
    except Exception:
        database_session.rollback()
    finally:
        database_session.close()
    if updated:
        for executor in _executors.values():
            executor.reset_runtime(context_id)
    return updated


def _record_session(context_id: str, agent: str, working_directory: str, first_message: str) -> None:
    assert _session_factory is not None
    database_session = _session_factory()
    try:
        if database_session.get(SessionRecord, context_id) is not None:
            return
        # Provisional title so the sidebar shows something immediately; an LLM-
        # generated title replaces it shortly via _finalize_session_title. The
        # sidebar truncates for display, so the full first line is stored.
        title = first_message.strip().split("\n", 1)[0]
        database_session.add(SessionRecord(
            id=context_id,
            agent=agent,
            working_directory=working_directory or "",
            title=title,
            created_at=datetime.now(timezone.utc).isoformat(),
        ))
        database_session.commit()
    finally:
        database_session.close()

    # Surface the new session immediately (its first turn is already marked
    # running, so the sidebar shows it with a spinner right away).
    _broadcaster.publish({"type": "sessions_changed"})

    try:
        task = asyncio.create_task(_finalize_session_title(context_id, first_message))
        _title_tasks.add(task)
        task.add_done_callback(_title_tasks.discard)
    except RuntimeError:
        # No running event loop (e.g. called outside a request) — keep the provisional title.
        pass


def _project_name(path: str) -> str:
    normalized = path.rstrip("/\\")
    return Path(normalized).name or normalized or path


def _path_scope(path_value: str, home_root: Path) -> str:
    """Whether a discovered file is ``global`` (under ``~/.agents``) or
    ``project`` (the selected folder's own ``.agents``)."""
    try:
        return "global" if Path(path_value).resolve().is_relative_to(home_root) else "project"
    except Exception:
        return "global"


def _record_project_path(path_value: str) -> str | None:
    directory = path_value.strip()
    if not directory:
        return None
    path = Path(directory).expanduser()
    if not path.is_absolute() or not path.exists() or not path.is_dir():
        return None

    resolved = str(path)
    assert _session_factory is not None
    database_session = _session_factory()
    try:
        record = database_session.get(ProjectHistoryRecord, resolved)
        selected_at = datetime.now(timezone.utc).isoformat()
        if record is None:
            database_session.add(ProjectHistoryRecord(
                path=resolved,
                name=_project_name(resolved),
                selected_at=selected_at,
            ))
        else:
            record.name = _project_name(resolved)
            record.selected_at = selected_at
        database_session.commit()
    finally:
        database_session.close()
    return resolved


def _record_model_selection(model_identifier: str) -> None:
    """Record a model selection in the history (upserting by id), mirroring the
    project-history list. Looks up the label/provider from the catalog so the UI
    can render recent models without re-resolving. No-op for an unknown id."""
    if not model_identifier or _session_factory is None:
        return
    definition = find_model(model_identifier)
    if definition is None:
        return
    database_session = _session_factory()
    try:
        record = database_session.get(ModelHistoryRecord, model_identifier)
        selected_at = datetime.now(timezone.utc).isoformat()
        if record is None:
            database_session.add(ModelHistoryRecord(
                model_id=model_identifier,
                name=definition.name,
                provider=definition.provider,
                selected_at=selected_at,
            ))
        else:
            record.name = definition.name
            record.provider = definition.provider
            record.selected_at = selected_at
        database_session.commit()
    except Exception:
        database_session.rollback()
    finally:
        database_session.close()


def _recent_models(limit: int = 8) -> list[dict[str, str]]:
    """Recently selected models, newest first."""
    if _session_factory is None:
        return []
    database_session = _session_factory()
    try:
        rows = (
            database_session.query(ModelHistoryRecord)
            .order_by(ModelHistoryRecord.selected_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {"id": row.model_id, "name": row.name, "provider": row.provider}
            for row in rows
        ]
    finally:
        database_session.close()


class SessionTitle(BaseModel):
    """Structured schema returned by the title-generation LLM call."""

    title: str = Field(
        description=(
            "A concise imperative phrase starting with a verb, then the action it describes; "
            "normal sentence case (not Title Case), no surrounding quotes, no trailing punctuation."
        ),
    )


# Loads the title prompt from the shared prompts directory next to the
# harness.core package, mirroring how AgentRuntime resolves its prompt loader.
_title_prompt_loader = PromptLoader(
    Path(_configuration.__file__).resolve().parent / "prompts"
)


async def _generate_session_title(first_message: str) -> str:
    """Ask the configured LLM for a short, structured title for the session.

    ``SessionTitle`` is bound as a tool with auto tool-choice rather than via
    ``with_structured_output``: the configured reasoning model rejects both
    ``response_format`` (json_schema) and the forced ``tool_choice`` that
    ``with_structured_output`` relies on, but accepts a regular tool call — the
    same pattern the main agent uses with ``bind_tools``.
    """
    assert _global_configuration is not None
    configuration = _global_configuration
    model_identifier = configuration.default_model_identifier()
    resolved = resolve_litellm(
        model_identifier,
        configuration.configured_provider_keys(),
        configuration.configured_provider_bases(),
    )
    if not resolved["api_key"]:
        return ""
    llm = ChatLiteLLMModel(
        model=resolved["model"],
        api_key=SecretStr(resolved["api_key"]),
        api_base=resolved["api_base"] or None,
        temperature=0,
    ).bind_tools([SessionTitle], tool_choice="auto")
    prompt = _title_prompt_loader.load("session_title", {})
    response = await llm.ainvoke([
        SystemMessage(content=prompt),
        HumanMessage(content=first_message),
    ])
    if not response.tool_calls:
        return ""
    title = SessionTitle.model_validate(response.tool_calls[0]["args"]).title
    return (title or "").strip()


async def _finalize_session_title(context_id: str, first_message: str) -> None:
    """Generate an LLM title for a new session and update the sidebar record."""
    assert _session_factory is not None
    try:
        title = await _generate_session_title(first_message)
    except Exception:
        return  # Keep the provisional title on any failure.
    if not title:
        return
    database_session = _session_factory()
    try:
        record = database_session.get(SessionRecord, context_id)
        if record is None or record.title == title:
            return
        record.title = title
        database_session.commit()
    finally:
        database_session.close()
    _broadcaster.publish({"type": "sessions_changed"})


def _card_for(agent_name: str, working_directory: str = ""):
    """Build an agent's AgentCard from its config and the skills available to it.

    When a ``working_directory`` is given, skills are scoped to that path (home
    globals plus the path's own ``.agents``, deduped) rather than the server's
    launch directory — so a card advertises the skills a session in that folder
    can actually find. Without one, the server-CWD scoping is used (startup mount)."""
    assert _global_configuration is not None
    configuration = load_agent_configuration(agent_name, _global_configuration.agent_directories())
    skill_roots = (
        _global_configuration.skill_directories_for(working_directory)
        if working_directory
        else _global_configuration.skill_directories()
    )
    all_skills = load_skills(skill_roots)
    agent_skills = skills_for_agent(all_skills, configuration.skills)
    return configuration, build_agent_card(configuration, agent_skills, PUBLIC_BASE_URL)


def _mount_agent(application: FastAPI, agent_name: str) -> None:
    """Serve one agent profile as its own A2A endpoint: its own executor, request
    handler, and AgentCard, mounted at a per-agent path. Idempotent."""
    assert _global_configuration is not None and _task_store is not None and _registry is not None
    _configuration, card = _card_for(agent_name)
    if agent_name in _mounted_agents:
        _registry.register(agent_name, _registry._handlers[agent_name], card)
        return
    executor = HarnessAgentExecutor(
        agent_name=agent_name,
        global_configuration=_global_configuration,
        task_store=_task_store,
        pending_permissions=_pending_permissions,
        pending_questions=_pending_questions,
        registry=_registry,
        on_new_context=_record_session,
        conversations=_conversations,
        on_turn_state=_set_turn_state,
        on_permission_state=_notify_permission_state,
        load_conversation=_load_conversation,
        save_conversation=_save_conversation,
        session_model_for=_session_model_for,
    )
    handler = DefaultRequestHandler(agent_executor=executor, task_store=_task_store)
    _executors[agent_name] = executor
    _registry.register(agent_name, handler, card)
    rpc_path = agent_rpc_path(agent_name)
    A2AFastAPIApplication(agent_card=card, http_handler=handler).add_routes_to_app(
        application,
        rpc_url=rpc_path,
        agent_card_url=f"{rpc_path}{AGENT_CARD_PATH}",
    )
    _mounted_agents.add(agent_name)


def _ensure_agents_for(working_directory: str) -> None:
    """Mount any agent the working directory declares that isn't mounted yet, so a
    folder's project-local agents become addressable A2A routes once that folder is
    selected. The route pool is shared and only grows — nothing is unmounted."""
    assert _global_configuration is not None
    directories = (
        _global_configuration.agent_directories_for(working_directory)
        if working_directory
        else _global_configuration.agent_directories()
    )
    for agent_name in list_agent_route_names(directories):
        if agent_name not in _mounted_agents:
            _mount_agent(app, agent_name)


def _reload_agent_cards() -> None:
    """Recompile AgentCards from the agent markdown and skill files so discovery
    reflects edits without a restart. Agent behaviour itself is already live,
    since each turn loads its configuration and skills fresh."""
    assert _global_configuration is not None and _registry is not None
    for agent_name in list_agent_route_names(_global_configuration.agent_directories()):
        handler = _registry._handlers.get(agent_name)
        if handler is not None:
            _configuration, card = _card_for(agent_name)
            _registry.register(agent_name, handler, card)


def _watched_a2a_paths() -> list[str]:
    assert _global_configuration is not None
    directories = [
        # The .agents roots are watched recursively so mcp.json (live MCP server
        # definitions) is picked up alongside the agents/ and skills/ subtrees.
        *_global_configuration.agents_root_directories(),
        *_global_configuration.agent_directories(),
        *_global_configuration.skill_directories(),
    ]
    watched: list[str] = []
    seen: set[Path] = set()
    for directory in directories:
        if not directory.is_dir():
            continue
        key = directory.resolve()
        if key in seen:
            continue
        seen.add(key)
        watched.append(str(key))
    return watched


async def _reload_mcp() -> None:
    """Re-read mcp.json and apply the server set live: reconcile the client manager
    (start new servers, stop removed/disabled ones, keep unchanged ones connected)
    and drop cached runtimes so the next turn rebuilds its tools with the new set.
    No server restart required."""
    global _mcp_manager
    assert _global_configuration is not None
    _global_configuration.mcp = GlobalConfiguration.load().mcp
    # Re-fold the startup-provisioned Composio server back in so a live mcp.json
    # edit doesn't drop Composio's tools (and the agent keeps its MCP tools).
    _global_configuration.mcp.servers.update(_composio_servers)
    enabled = _global_configuration.mcp.enabled_servers()
    if _mcp_manager is None:
        if enabled:
            _mcp_manager = MCPClientManager(enabled)
            await _mcp_manager.start()
            set_mcp_client_manager(_mcp_manager)
    else:
        await _mcp_manager.reconcile(enabled)
    for executor in _executors.values():
        executor.reset_runtimes()


async def _ensure_mcp_servers_for(working_directory: str) -> None:
    """Additively grow the shared MCP server pool with the working directory's own
    ``mcp.json`` servers, so a folder's servers are running and listable once that
    folder is selected. The pool is a union — servers are only added or updated,
    never removed — so no other session loses its servers."""
    global _mcp_manager
    assert _global_configuration is not None
    if not working_directory:
        return
    folder_servers = _global_configuration.mcp_configuration_for(working_directory).servers
    new_servers = {
        name: configuration
        for name, configuration in folder_servers.items()
        if _global_configuration.mcp.servers.get(name) != configuration
    }
    if not new_servers:
        return
    _global_configuration.mcp.servers.update(new_servers)
    enabled = _global_configuration.mcp.enabled_servers()
    if _mcp_manager is None:
        if enabled:
            _mcp_manager = MCPClientManager(enabled)
            await _mcp_manager.start()
            set_mcp_client_manager(_mcp_manager)
    else:
        await _mcp_manager.reconcile(enabled)
    for executor in _executors.values():
        executor.reset_runtimes()


async def _watch_agents_and_skills(application: FastAPI) -> None:
    """Watch the agents, skills, and mcp.json sources; on any change, mount newly
    added agents, reload MCP servers, refresh cards, and broadcast so connected
    clients refetch immediately. Agents, skills, and MCP servers are all picked up
    live, so the only thing needing a restart is a change to the core harness."""
    assert _global_configuration is not None
    watched = _watched_a2a_paths()
    if not watched:
        return
    try:
        async for changes in awatch(*watched):
            if any(str(path).endswith("mcp.json") for _change, path in changes):
                await _reload_mcp()
            for agent_name in list_agent_route_names(_global_configuration.agent_directories()):
                if agent_name not in _mounted_agents:
                    _mount_agent(application, agent_name)
            _reload_agent_cards()
            _broadcaster.publish({"type": "agents_changed"})
    except asyncio.CancelledError:
        pass


@asynccontextmanager
async def lifespan(application: FastAPI):
    global _global_configuration, _session_factory, _async_engine, _task_store, _registry, _mcp_manager, _composio_servers
    _global_configuration = GlobalConfiguration.load()

    database_path = database_file_path()
    sync_engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    _session_factory = sessionmaker(bind=sync_engine)

    exa_key = _global_configuration.exa.effective_api_key
    if exa_key:
        from exa_py import Exa
        set_exa_client(Exa(api_key=exa_key))

    # Provision the Composio Tool Router (best-effort) and fold it into the MCP
    # config itself, so both the client manager and the agent's tool gating
    # (which binds list_mcp_tools/call_mcp_tool only when a server is configured)
    # see it — Composio's tools then ride the normal MCP path.
    _composio_servers = composio_mcp_servers(_global_configuration.composio)
    _global_configuration.mcp.servers.update(_composio_servers)
    mcp_servers = _global_configuration.mcp.enabled_servers()
    _mcp_manager = MCPClientManager(mcp_servers) if mcp_servers else None
    if _mcp_manager is not None:
        await _mcp_manager.start()
    set_mcp_client_manager(_mcp_manager)

    _async_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    _task_store = AppendOnlyTaskStore(_async_engine)
    await _task_store.initialize()

    _registry = AgentRegistry(_task_store)
    for agent_name in list_agent_route_names(_global_configuration.agent_directories()):
        _mount_agent(application, agent_name)

    watcher = asyncio.create_task(_watch_agents_and_skills(application))
    try:
        yield
    finally:
        watcher.cancel()
        cancel_all_background_tasks()
        if _mcp_manager is not None:
            await _mcp_manager.aclose()


app = FastAPI(title="harness", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


class AgentInfo(BaseModel):
    id: str
    name: str
    title: str = ""


class AgentsList(BaseModel):
    agents: list[AgentInfo]


class DirectoryValidationRequest(BaseModel):
    directory: str


class RecentProjectRequest(BaseModel):
    path: str


class PermissionRequest(BaseModel):
    request_id: str
    decision: str


class QuestionRequest(BaseModel):
    request_id: str
    # One entry per question, in order: a list of selected labels (plus any
    # custom text the user typed). The runtime returns this verbatim to the tool.
    answers: list[Any]


class SteeringRequest(BaseModel):
    message: str


class PermissionModeRequest(BaseModel):
    mode: Literal["default", "auto", "read_only", "bypass"]


class SettingsUpdateRequest(BaseModel):
    exa_api_key: str = ""
    composio_consumer_api_key: str = ""
    # Per-provider API keys (the opencode gateway's key lives under "opencode").
    provider_keys: dict[str, str] = {}
    # Base URLs for the OpenAI-compatible providers (opencode, custom).
    provider_base_urls: dict[str, str] = {}
    # The default model id (provider/model) used when a session has no override.
    default_model: str = ""


class SandboxUpdateRequest(BaseModel):
    enabled: bool


class MCPToolCallRequest(BaseModel):
    server: str
    tool_name: str
    arguments: dict = {}


class MCPResourceReadRequest(BaseModel):
    server: str
    uri: str


@app.get("/agents")
async def agents(working_directory: str = ""):
    """List agent profiles for the UI selector, scoped to the selected folder:
    the home globals plus that folder's own ``.agents/agents`` (deduped), never
    the directory the server was launched in. Passing ``working_directory`` is
    what makes the list track the chosen folder."""
    assert _global_configuration is not None
    if working_directory:
        _ensure_agents_for(working_directory)
        directories = _global_configuration.agent_directories_for(working_directory)
    else:
        directories = _global_configuration.agent_directories()
    agent_data = list_agents(directories)
    return AgentsList(agents=[AgentInfo(id=agent["id"], name=agent["name"], title=agent.get("title", agent["name"])) for agent in agent_data])


@app.get("/agents/cards")
async def agent_cards(working_directory: str = ""):
    """Discovery: the full A2A AgentCard for every served agent, including their
    skills, so the UI can broadcast what each agent can do.

    Skills are scoped to ``working_directory`` when given: the home globals plus
    that path's own ``.agents`` skills (deduped), and crucially *not* the skills of
    the directory the server happens to have been launched in. The UI passes the
    selected project path so the advertised skills match what a session there can
    actually find, refreshing whenever the user picks a different folder."""
    assert _registry is not None and _global_configuration is not None
    skill_roots = (
        _global_configuration.skill_directories_for(working_directory)
        if working_directory
        else _global_configuration.skill_directories()
    )
    all_skills = load_skills(skill_roots)
    skill_titles = {skill.identifier: skill.display_title for skill in all_skills}
    skill_enabled = {skill.identifier: skill.enabled for skill in all_skills}
    # Cards are served from the shared (union) route pool, but listed only for the
    # agents the selected folder actually declares (home globals plus that folder's
    # own), so the launch directory's agents don't leak into an unrelated folder.
    allowed_agents: set[str] | None = None
    if working_directory:
        _ensure_agents_for(working_directory)
        allowed_agents = {
            agent["id"]
            for agent in list_agents(_global_configuration.agent_directories_for(working_directory))
        }
    cards: list[dict] = []
    for existing in _registry.cards():
        agent_name = str(existing.name or "")
        if allowed_agents is not None and agent_name not in allowed_agents:
            continue
        try:
            configuration, card = _card_for(agent_name, working_directory)
            title = configuration.display_name
        except Exception:
            card, title = existing, agent_name
        dumped = card.model_dump(by_alias=True, exclude_none=True, mode="json")
        dumped["title"] = title
        for skill in dumped.get("skills", []):
            if isinstance(skill, dict):
                skill_name = str(skill.get("name") or skill.get("id") or "")
                skill["title"] = skill_titles.get(skill_name, skill_name)
                skill["enabled"] = skill_enabled.get(skill_name, True)
        cards.append(dumped)
    return {"cards": cards}


@app.get("/skills")
async def skills(working_directory: str = ""):
    """List the skills available in the selected folder — home globals plus that
    folder's own ``.agents/skills`` (deduped), never the launch directory. This is
    independent of any agent, so the UI can show a folder's skills even when it has
    no agents. Disabled skills are returned (flagged) so the UI greys them out."""
    assert _global_configuration is not None
    roots = (
        _global_configuration.skill_directories_for(working_directory)
        if working_directory
        else _global_configuration.skill_directories()
    )
    all_skills = load_skills(roots)
    home_root = _global_configuration.home_agents_root().resolve()
    return {
        "skills": [
            {
                "id": skill.identifier,
                "name": skill.identifier,
                "title": skill.display_title,
                "description": skill.description,
                "enabled": skill.enabled,
                "scope": _path_scope(skill.path, home_root),
            }
            for skill in all_skills
        ]
    }


@app.get(AGENT_CARD_PATH)
async def default_agent_card():
    """Serve the default agent's card at the well-known path for spec compliance."""
    assert _registry is not None and _global_configuration is not None
    card = _registry.card(_global_configuration.default_agent) or (
        _registry.cards()[0] if _registry.cards() else None
    )
    if card is None:
        return {}
    return card.model_dump(by_alias=True, exclude_none=True, mode="json")


@app.get("/home")
async def home_directory():
    """The server user's home directory and its folder name — the default project
    the UI selects before anything else is chosen."""
    home = str(Path.home())
    return {"path": home, "name": _project_name(home)}


@app.get("/projects/recent")
async def recent_projects():
    """Recent working directories selected in the UI or used by sessions."""
    assert _session_factory is not None
    database_session = _session_factory()
    try:
        projects: dict[str, dict[str, str]] = {}
        project_rows = database_session.query(ProjectHistoryRecord).order_by(ProjectHistoryRecord.selected_at.desc()).all()
        for row in project_rows:
            if row.path:
                projects[row.path] = {
                    "path": row.path,
                    "name": row.name or _project_name(row.path),
                    "last_used_at": row.selected_at,
                }

        session_rows = database_session.query(SessionRecord).order_by(SessionRecord.created_at.desc()).all()
        for row in session_rows:
            path = (row.working_directory or "").strip()
            if not path:
                continue
            existing = projects.get(path)
            if existing is None or row.created_at > existing["last_used_at"]:
                projects[path] = {
                    "path": path,
                    "name": _project_name(path),
                    "last_used_at": row.created_at,
                }

        return {
            "projects": sorted(projects.values(), key=lambda project: project["last_used_at"], reverse=True)
        }
    finally:
        database_session.close()


@app.post("/projects/recent")
async def record_recent_project(request: RecentProjectRequest):
    """Record a validated working directory selection."""
    path = _record_project_path(request.path)
    if not path:
        return {"saved": False}
    _broadcaster.publish({"type": "projects_changed"})
    return {"saved": True, "path": path, "name": _project_name(path)}


@app.get("/models")
async def list_models_endpoint():
    """The model catalog for the picker: every curated model with its provider and
    whether its provider has a resolvable credential (available), plus the provider
    registry and the current default model. Available models are fronted in the
    picker; locked ones stay listed (greyed) so the user sees what a key unlocks."""
    assert _global_configuration is not None
    configured_keys = _global_configuration.configured_provider_keys()
    available_identifiers = {model.identifier for model in available_models(configured_keys)}
    models = [
        {
            "id": model.identifier,
            "name": model.name,
            "provider": model.provider,
            "available": model.identifier in available_identifiers,
        }
        for model in MODELS
    ]
    providers = [
        {
            "id": provider.identifier,
            "name": provider.name,
            "openai_compatible": provider.openai_compatible,
        }
        for provider in PROVIDERS.values()
    ]
    return {
        "models": models,
        "providers": providers,
        "default_model": _global_configuration.default_model_identifier(),
    }


@app.get("/models/recent")
async def recent_models():
    """Recently selected models (newest first), mirroring the project history — a
    user can quickly switch back to a model they used before without scrolling the
    full catalog. Each entry is only recorded once it is actually selected."""
    return {"models": _recent_models()}


@app.get("/settings")
async def get_settings():
    """Return the API credentials stored in ~/.harness/configuration.yaml so the
    settings dialog can pre-fill them, including per-provider keys and the default
    model."""
    assert _global_configuration is not None
    return {
        "exa_api_key": _global_configuration.exa.api_key,
        "composio_consumer_api_key": _global_configuration.composio.consumer_api_key,
        "sandbox_enabled": _global_configuration.sandbox.enabled,
        "default_model": _global_configuration.default_model_identifier(),
        "providers": {
            identifier: {"api_key": credential.api_key, "base_url": credential.base_url}
            for identifier, credential in _global_configuration.providers.items()
        },
    }


@app.post("/settings")
async def update_settings(request: SettingsUpdateRequest):
    """Persist API credentials to ~/.harness/configuration.yaml and apply them
    live: refresh the in-memory configuration, the Exa client, restart the MCP
    client manager so Composio tools appear/disappear with its key, and drop
    cached agent runtimes so the next turn rebuilds with the new credentials."""
    global _composio_servers, _mcp_manager
    assert _global_configuration is not None
    configuration = _global_configuration
    save_api_keys(
        exa_api_key=request.exa_api_key,
        composio_consumer_api_key=request.composio_consumer_api_key,
        provider_keys=request.provider_keys,
        provider_base_urls=request.provider_base_urls,
        default_model=request.default_model,
    )
    configuration.exa.api_key = request.exa_api_key
    configuration.composio.consumer_api_key = request.composio_consumer_api_key
    # Rebuild the providers map from the posted keys/base URLs, merging so a
    # provider the dialog did not render keeps its stored credential.
    merged_providers = {
        identifier: _configuration.ProviderCredential(
            api_key=credential.api_key, base_url=credential.base_url
        )
        for identifier, credential in configuration.providers.items()
    }
    for provider_identifier, api_key in request.provider_keys.items():
        existing = merged_providers.get(provider_identifier) or _configuration.ProviderCredential()
        merged_providers[provider_identifier] = existing.model_copy(update={"api_key": api_key})
    for provider_identifier, base_url in request.provider_base_urls.items():
        existing = merged_providers.get(provider_identifier) or _configuration.ProviderCredential()
        merged_providers[provider_identifier] = existing.model_copy(update={"base_url": base_url})
    configuration.providers = merged_providers
    if request.default_model:
        # The picker carries the combined ``provider/model`` id; split it into the
        # two separate in-memory fields (mirroring how save_api_keys persists them).
        if "/" in request.default_model:
            provider, model = request.default_model.split("/", 1)
            configuration.default_provider = provider
            configuration.default_model = model
        else:
            configuration.default_model = request.default_model
        _record_model_selection(request.default_model)

    exa_key = configuration.exa.effective_api_key
    if exa_key:
        from exa_py import Exa
        set_exa_client(Exa(api_key=exa_key))
    else:
        set_exa_client(None)

    # Re-provision Composio now that its key may have changed: rebuild the server
    # config, fold it into (or remove it from) the MCP config, and restart the
    # MCP client manager so the agent picks Composio tools up live.
    _composio_servers = composio_mcp_servers(configuration.composio)
    if _composio_servers:
        configuration.mcp.servers.update(_composio_servers)
    else:
        configuration.mcp.servers.pop(configuration.composio.server_name, None)
    if _mcp_manager is not None:
        await _mcp_manager.aclose()
    mcp_servers = configuration.mcp.enabled_servers()
    _mcp_manager = MCPClientManager(mcp_servers) if mcp_servers else None
    if _mcp_manager is not None:
        await _mcp_manager.start()
    set_mcp_client_manager(_mcp_manager)

    for executor in _executors.values():
        executor.reset_runtimes()
    return {"status": "saved"}


@app.post("/settings/sandbox")
async def update_sandbox(request: SandboxUpdateRequest):
    """Persist and apply the sandbox toggle independently from credentials."""
    assert _global_configuration is not None
    save_api_keys(sandbox_enabled=request.enabled)
    _global_configuration.sandbox.enabled = request.enabled
    for executor in _executors.values():
        executor.reset_runtimes()
    return {"status": "saved", "sandbox_enabled": _global_configuration.sandbox.enabled}


@app.get("/mcp/tools")
async def mcp_tools(server: str = "", working_directory: str = ""):
    """List configured MCP servers with their enabled flag. Enabled servers carry
    their advertised tools; disabled ones are still returned (with no tools) so the
    UI can show them greyed out rather than hiding them.

    Scoped to the selected folder when ``working_directory`` is given: only the
    servers that folder declares (its own ``mcp.json`` plus the home globals) and
    the global Composio integration are listed — the launch directory's servers do
    not leak in. The folder's servers are ensured running first so their tools
    actually appear (the subprocess pool is shared and grows as a union)."""
    assert _global_configuration is not None
    # Servers declared by the working directory's own mcp.json are project-specific;
    # everything else (home globals and the Composio integration) is global.
    project_server_names: set[str] = set()
    if working_directory:
        await _ensure_mcp_servers_for(working_directory)
        allowed = set(_global_configuration.mcp_configuration_for(working_directory).servers)
        allowed.update(_composio_servers)
        configured = {
            name: configuration
            for name, configuration in _global_configuration.mcp.servers.items()
            if name in allowed
        }
        home_root = _global_configuration.home_agents_root().resolve()
        project_root = _global_configuration.project_agents_root_for(working_directory).resolve()
        if project_root != home_root:
            project_server_names = set(
                _configuration.MCPConfiguration.from_dotagents_roots([project_root]).servers
            )
    else:
        configured = _global_configuration.mcp.servers
    tools_by_server: dict[str, list] = {}
    if _mcp_manager is not None:
        # List every enabled server, then filter below — querying the manager for a
        # disabled server name would raise, since it only holds enabled ones.
        listing = await _mcp_manager.list_tools("")
        tools_by_server = {entry["name"]: entry["tools"] for entry in listing["servers"]}
    servers = [
        {
            "name": name,
            "enabled": configuration.enabled,
            "tools": tools_by_server.get(name, []),
            "scope": "project" if name in project_server_names else "global",
        }
        for name, configuration in sorted(configured.items())
        if not server or name == server
    ]
    return {"servers": servers}


@app.get("/mcp/resources")
async def mcp_resources(server: str = ""):
    """List resources exposed by configured MCP servers."""
    if _mcp_manager is None:
        return {"servers": []}
    return await _mcp_manager.list_resources(server)


@app.post("/mcp/tools/call")
async def mcp_call_tool(request: MCPToolCallRequest):
    """Call a configured MCP server tool. Intended for smoke tests and UI discovery."""
    if _mcp_manager is None:
        return {"error": "MCP is not configured."}
    return await _mcp_manager.call_tool(request.server, request.tool_name, request.arguments)


@app.post("/mcp/resources/read")
async def mcp_read_resource(request: MCPResourceReadRequest):
    """Read a configured MCP resource. Intended for smoke tests and UI discovery."""
    if _mcp_manager is None:
        return {"error": "MCP is not configured."}
    return await _mcp_manager.read_resource(request.server, request.uri)


@app.post("/directory/validate")
async def validate_directory(request: DirectoryValidationRequest):
    """Validate that a path is an existing absolute directory."""
    directory = request.directory.strip()
    if not directory:
        return {"valid": False, "exists": False, "is_directory": False, "is_absolute": False, "path": ""}
    path = Path(directory).expanduser()
    return {
        "valid": path.is_absolute() and path.exists() and path.is_dir(),
        "exists": path.exists(),
        "is_directory": path.is_dir(),
        "is_absolute": path.is_absolute(),
        "path": str(path),
    }


@app.post("/directory/browse")
async def browse_directory():
    """Open a native folder picker on the local server machine and return an absolute path."""
    system = platform.system()
    try:
        if system == "Darwin":
            result = subprocess.run(
                ["osascript", "-e", 'POSIX path of (choose folder with prompt "Choose a working directory")'],
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
            return _folder_picker_result(result)
        if system == "Windows":
            command = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
                "$dialog.Description = 'Choose a working directory'; "
                "$dialog.ShowNewFolderButton = $true; "
                "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
                "{ $dialog.SelectedPath }"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", command],
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
            return _folder_picker_result(result)
        result = _run_unix_folder_picker()
        if result is not None:
            return _folder_picker_result(result)
        return {
            "path": "",
            "cancelled": True,
            "error": "No supported graphical folder picker is available.",
        }
    except subprocess.TimeoutExpired:
        return {"path": "", "cancelled": True, "error": "Folder selection timed out."}
    except FileNotFoundError as exception:
        return {"path": "", "cancelled": True, "error": f"Folder picker is unavailable: {exception.filename}"}


def _folder_picker_result(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    if result.returncode != 0:
        return {"path": "", "cancelled": True, "error": result.stderr.strip()}
    selected_path = result.stdout.strip()
    if not selected_path:
        return {"path": "", "cancelled": True}
    return {"path": str(Path(selected_path).expanduser().resolve()), "cancelled": False}


def _run_unix_folder_picker() -> subprocess.CompletedProcess[str] | None:
    if shutil.which("zenity"):
        return subprocess.run(
            ["zenity", "--file-selection", "--directory", "--title=Choose a working directory"],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    if shutil.which("kdialog"):
        return subprocess.run(
            ["kdialog", "--getexistingdirectory", str(Path.home())],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    return _run_tk_folder_picker()


def _run_tk_folder_picker() -> subprocess.CompletedProcess[str] | None:
    script = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk()\n"
        "root.withdraw()\n"
        "path = filedialog.askdirectory(title='Choose a working directory')\n"
        "print(path or '')\n"
        "root.destroy()\n"
    )
    try:
        return subprocess.run(
            ["python3", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None


@app.get("/sessions")
async def list_sessions():
    """List recent chat sessions for the sidebar."""
    assert _session_factory is not None
    database_session = _session_factory()
    try:
        rows = database_session.query(SessionRecord).order_by(SessionRecord.created_at.desc()).limit(50).all()
        return {
            "sessions": [
                {
                    "session_id": row.id,
                    "agent": row.agent,
                    "title": row.title,
                    "created_at": row.created_at,
                    "working_directory": row.working_directory,
                    "working_directory_name": _project_name(row.working_directory) if row.working_directory else "",
                    "model": row.model or "",
                    "running": row.id in _running_contexts,
                    "awaiting_input": any(
                        request_id.startswith(f"perm-{row.id}-") and not future.done()
                        for request_id, future in _pending_permissions.items()
                    ),
                }
                for row in rows
            ]
        }
    finally:
        database_session.close()


@app.get("/sessions/{context_id}/tasks")
async def session_tasks(context_id: str):
    """All A2A tasks for a context — the main turn tasks (with history and
    artifacts) plus related sub-agent tasks — for replaying a session."""
    assert _task_store is not None
    tasks = await _task_store.tasks_for_context(context_id)
    return {
        "tasks": [
            task.model_dump(by_alias=True, exclude_none=True, mode="json")
            for task in tasks
        ]
    }


class SessionModelRequest(BaseModel):
    model: str


@app.put("/sessions/{context_id}/model")
async def update_session_model(context_id: str, request: SessionModelRequest):
    """Set or clear a per-session model override (provider/model id, or "" to fall
    back to the global default). Persists to the sessions table and drops the cached
    runtime so the next turn runs on the new model. A non-empty selection is also
    recorded in the model history for quick switching."""
    updated = _set_session_model(context_id, request.model)
    if not updated:
        raise HTTPException(status_code=404, detail="Session not found.")
    if request.model:
        _record_model_selection(request.model)
    return {"status": "saved", "model": request.model or ""}


@app.get("/sessions/{context_id}/model")
async def get_session_model(context_id: str):
    """The per-session model override for a context ("" = global default)."""
    return {"model": _session_model_for(context_id)}


@app.get("/preview/{file_path:path}")
async def preview_file(file_path: str):
    """Serve a local file for an ``open_web_preview`` artifact (the UI points a
    sandboxed iframe here). HTML gets the widget runtime injected so a previewed
    page can self-size, report render errors, and be interactive; everything else
    (images, PDFs, CSS/JS assets a page references) is served verbatim so relative
    links inside a previewed page resolve. Localhost-only, like the rest of the API."""
    path = Path("/" + file_path.lstrip("/")).resolve()
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    suffix = path.suffix.lower()
    # Previews are live local files being iterated on, and a page's sibling assets
    # (CSS/JS/images) are fetched by their stable paths without the cache-busting
    # version the iframe URL carries — so serve everything no-store, or a refresh
    # would reload the HTML but keep showing cached assets.
    no_store = {"Cache-Control": "no-store"}
    if suffix in (".html", ".htm", ".xhtml"):
        try:
            markup = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise HTTPException(status_code=400, detail=f"Could not read file: {error}")
        return HTMLResponse(_inject_widget_runtime(markup), headers=no_store)
    return FileResponse(path, headers=no_store)


# A rewriting pass-through proxy for `open_web_preview` of external URLs. It serves
# the page — and *every* asset and request it makes — back through this one route,
# so to the framed page everything looks same-origin (our localhost). That is what
# lets sites that refuse direct framing (`X-Frame-Options`/`frame-ancestors`) render,
# and avoids the cross-origin CORS/history errors a naive `<base>` proxy hits.

_PROXY_PATH = "/preview-proxy"
_PROXY_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
# URL schemes that must never be rewritten through the proxy.
_PROXY_SKIP_SCHEMES = ("data:", "blob:", "javascript:", "mailto:", "tel:", "about:", "#", "vbscript:")
# Response headers dropped when re-serving (framing blockers + hop-by-hop/encoding
# headers that no longer match the rewritten body).
_PROXY_DROP_HEADERS = {
    "x-frame-options",
    "content-security-policy",
    "content-security-policy-report-only",
    "cross-origin-opener-policy",
    "cross-origin-embedder-policy",
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
    "set-cookie",
}

_PROXY_HTML_ATTR_RE = re.compile(
    r'(?P<pre>\b(?:src|href|action|formaction|poster|data-src|data-href|data-url)\s*=\s*)'
    r'(?P<q>["\'])(?P<url>[^"\']*)(?P=q)',
    re.IGNORECASE,
)
_PROXY_HTML_SRCSET_RE = re.compile(r'(?P<pre>\bsrcset\s*=\s*)(?P<q>["\'])(?P<val>[^"\']*)(?P=q)', re.IGNORECASE)
_PROXY_STYLE_BLOCK_RE = re.compile(r'(<style[^>]*>)(?P<body>.*?)(</style>)', re.IGNORECASE | re.DOTALL)
_PROXY_CSS_URL_RE = re.compile(r'url\(\s*(?P<q>["\']?)(?P<url>[^)"\']+)(?P=q)\s*\)', re.IGNORECASE)
_PROXY_CSS_IMPORT_RE = re.compile(r'(?P<pre>@import\s+)(?P<q>["\'])(?P<url>[^"\']+)(?P=q)', re.IGNORECASE)
# Tags/attributes that would fight the proxy: an inline CSP, a <base> that would
# re-point relative URLs, and SRI/crossorigin hints that fail once same-origin.
_PROXY_CSP_META_RE = re.compile(r'<meta[^>]+http-equiv\s*=\s*["\']?content-security-policy[^>]*>', re.IGNORECASE)
_PROXY_BASE_TAG_RE = re.compile(r'<base[^>]*>', re.IGNORECASE)
_PROXY_STRIP_ATTR_RE = re.compile(
    r'\s+(?:integrity|crossorigin|nonce)(?:\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+))?',
    re.IGNORECASE,
)


def _proxy_ref(raw: str, base: str) -> str:
    """Resolve ``raw`` (possibly relative) against ``base`` and route it back through
    this proxy. Schemes that are not real fetches (data:, javascript:, #…) pass through."""
    target = raw.strip()
    if not target or target.lower().startswith(_PROXY_SKIP_SCHEMES):
        return raw
    absolute = urljoin(base, target)
    if not absolute.lower().startswith(("http://", "https://")):
        return raw
    return f"{_PROXY_PATH}?url={quote(absolute, safe='')}"


def _rewrite_proxy_css(text: str, base: str) -> str:
    text = _PROXY_CSS_URL_RE.sub(
        lambda m: f'url({m.group("q")}{_proxy_ref(m.group("url"), base)}{m.group("q")})', text
    )
    text = _PROXY_CSS_IMPORT_RE.sub(
        lambda m: f'{m.group("pre")}{m.group("q")}{_proxy_ref(m.group("url"), base)}{m.group("q")}', text
    )
    return text


def _rewrite_proxy_srcset(value: str, base: str) -> str:
    rewritten = []
    for candidate in value.split(","):
        chunk = candidate.strip()
        if not chunk:
            continue
        bits = chunk.split(None, 1)
        descriptor = f" {bits[1]}" if len(bits) > 1 else ""
        rewritten.append(f"{_proxy_ref(bits[0], base)}{descriptor}")
    return ", ".join(rewritten)


def _proxy_runtime(base: str) -> str:
    """A small shim injected into every proxied page so URLs built *by scripts*
    (fetch/XHR, history navigations, dynamically created elements) also go through
    the proxy and resolve against the real origin — not our localhost — and so a
    cross-origin ``history.replaceState`` no longer throws."""
    base_json = json.dumps(base)
    proxy_json = json.dumps(f"{_PROXY_PATH}?url=")
    return (
        "<script>(function(){"
        f"var BASE={base_json};var PROXY={proxy_json};"
        "function abs(u){try{return new URL(u,BASE).href;}catch(e){return null;}}"
        "function prox(u){if(typeof u!=='string'||!u)return u;"
        "if(/^(data:|blob:|javascript:|about:|mailto:|tel:|#)/i.test(u))return u;"
        "if(u.indexOf(PROXY)!==-1)return u;var a=abs(u);"
        "if(!a||!/^https?:/i.test(a))return u;return PROXY+encodeURIComponent(a);}"
        "['pushState','replaceState'].forEach(function(m){var o=history[m];history[m]=function(s,t,u){"
        "try{return o.call(history,s,t,u);}catch(e){try{return o.call(history,s,t);}catch(_){}}};});"
        "if(window.fetch){var of=window.fetch;window.fetch=function(i,n){try{"
        "if(typeof i==='string')i=prox(i);else if(i&&i.url)i=new Request(prox(i.url),i);}catch(e){}"
        "return of.call(this,i,n);};}"
        "if(window.XMLHttpRequest){var oo=XMLHttpRequest.prototype.open;"
        "XMLHttpRequest.prototype.open=function(m,u){try{u=prox(u);}catch(e){}"
        "return oo.apply(this,[m,u].concat([].slice.call(arguments,2)));};}"
        "})();</script>"
    )


def _rewrite_proxy_html(markup: str, base: str) -> str:
    markup = _PROXY_CSP_META_RE.sub("", markup)
    markup = _PROXY_BASE_TAG_RE.sub("", markup)
    markup = _PROXY_STRIP_ATTR_RE.sub("", markup)
    markup = _PROXY_HTML_ATTR_RE.sub(
        lambda m: f'{m.group("pre")}{m.group("q")}{_proxy_ref(m.group("url"), base)}{m.group("q")}', markup
    )
    markup = _PROXY_HTML_SRCSET_RE.sub(
        lambda m: f'{m.group("pre")}{m.group("q")}{_rewrite_proxy_srcset(m.group("val"), base)}{m.group("q")}', markup
    )
    markup = _PROXY_STYLE_BLOCK_RE.sub(
        lambda m: f'{m.group(1)}{_rewrite_proxy_css(m.group("body"), base)}{m.group(3)}', markup
    )
    runtime = _proxy_runtime(base)
    head_match = re.search(r"<head[^>]*>", markup, re.IGNORECASE)
    if head_match:
        return markup[: head_match.end()] + runtime + markup[head_match.end() :]
    return runtime + markup


@app.get("/preview-proxy")
async def preview_proxy(url: str):
    """Fetch an external ``http(s)`` URL server-side and re-serve it (and everything
    it links to) from our own origin, rewritten so the framed page behaves as if it
    were same-origin. This is what makes ``open_web_preview`` a real mini-browser for
    sites that block direct framing. Localhost-only, like the rest of the API."""
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Only http(s) URLs can be previewed.")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            upstream = await client.get(url, headers=_PROXY_BROWSER_HEADERS)
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"Could not load {url}: {error}")

    final_url = str(upstream.url)
    content_type = upstream.headers.get("content-type", "application/octet-stream")
    lowered = content_type.lower()
    if "html" in lowered:
        return HTMLResponse(_rewrite_proxy_html(upstream.text, final_url))
    if "css" in lowered:
        return Response(_rewrite_proxy_css(upstream.text, final_url), media_type=content_type)
    # Scripts, images, fonts, JSON, … — re-serve verbatim from our origin (httpx has
    # already decoded any transfer encoding), minus the headers that no longer apply.
    safe_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _PROXY_DROP_HEADERS
    }
    return Response(content=upstream.content, media_type=content_type, headers=safe_headers)


@app.get("/events")
async def events():
    """Server-sent live events (e.g. agents changed) for the UI to react to."""
    queue = _broadcaster.subscribe()

    async def event_generator():
        try:
            while True:
                event = await queue.get()
                yield {"event": "message", "data": json.dumps(event)}
        finally:
            _broadcaster.unsubscribe(queue)

    return EventSourceResponse(event_generator())


@app.post("/chat/{context_id}/permission")
async def resolve_permission(context_id: str, request: PermissionRequest):
    """Resolve a pending human-in-the-loop permission request. ``deny`` rejects;
    ``allow_once`` and ``allow_always`` both let this command run (``allow_always``
    additionally records a session rule — handled separately)."""
    future = _pending_permissions.get(request.request_id)
    if not future:
        return {"status": "unknown", "error": "No pending permission request with that identifier."}
    if future.done():
        return {"status": "stale", "error": "Permission request was already resolved."}
    # The runtime resumes on the decision string ("deny" / "allow_once" /
    # "allow_always") so it can record a session rule for "allow_always".
    future.set_result(request.decision)
    # The session is no longer waiting — refresh the sidebar marker.
    _broadcaster.publish({"type": "sessions_changed"})
    return {"status": "resolved", "decision": request.decision}


@app.post("/chat/{context_id}/question")
async def resolve_question(context_id: str, request: QuestionRequest):
    """Resolve a pending ask_user request with the user's answers."""
    future = _pending_questions.get(request.request_id)
    if not future:
        return {"status": "unknown", "error": "No pending question with that identifier."}
    if future.done():
        return {"status": "stale", "error": "Question was already resolved."}
    future.set_result(request.answers)
    _broadcaster.publish({"type": "sessions_changed"})
    return {"status": "resolved", "answers": request.answers}


@app.post("/chat/{context_id}/steer")
async def steer_context(context_id: str, request: SteeringRequest):
    """Append user steering to an active turn at the next model-call boundary."""
    message = request.message.strip()
    if not message:
        return {"queued": False}
    for executor in _executors.values():
        if executor.steer_context(context_id, message):
            return {"queued": True}
    raise HTTPException(status_code=409, detail="Session is not currently steerable.")


@app.post("/chat/{context_id}/abort")
async def abort_session(context_id: str):
    """Abort the running turn for a context and reject any pending permissions."""
    prefix = f"perm-{context_id}-"
    for request_id, future in list(_pending_permissions.items()):
        if request_id.startswith(prefix) and not future.done():
            future.set_result("deny")
    q_prefix = f"q-{context_id}-"
    for request_id, future in list(_pending_questions.items()):
        if request_id.startswith(q_prefix) and not future.done():
            # A cancelled question resolves to an empty answer list so the
            # awaiting tool call completes cleanly instead of hanging.
            future.set_result([])
    aborted = any(executor.abort_context(context_id) for executor in _executors.values())
    return {"status": "aborted" if aborted else "not_found", "session_id": context_id}


@app.post("/chat/{context_id}/permissions/mode")
async def set_permission_mode(context_id: str, request: PermissionModeRequest):
    """Set the permission mode for a context's agent."""
    updated = any(executor.set_permission_mode(context_id, request.mode) for executor in _executors.values())
    return {"status": "updated" if updated else "not_found", "mode": request.mode}


def run_server(host: str = "127.0.0.1", port: int = 8822):
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8822)
