import asyncio
import json
import platform
import shutil
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import Column, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sse_starlette.sse import EventSourceResponse
from watchfiles import awatch
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

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
from harness.core.mcp_client import MCPClientManager
from harness.core.skills import load_skills, skills_for_agent
from harness.tools.tools import cancel_all_background_tasks, set_exa_client, set_mcp_client_manager

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
    created_at = Column(String, nullable=False)


class ProjectHistoryRecord(Base):
    """Recent working directories selected in the UI."""

    __tablename__ = "project_history"

    path = Column(Text, primary_key=True)
    name = Column(Text, default="")
    selected_at = Column(String, nullable=False)


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
_executors: dict[str, HarnessAgentExecutor] = {}
_mounted_agents: set[str] = set()
_pending_permissions: dict[str, asyncio.Future] = {}
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
_broadcaster = Broadcaster()
# Keeps references to in-flight session-title generation tasks so they are not
# garbage-collected before completing.
_title_tasks: set[asyncio.Task] = set()


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
    api = _global_configuration.api
    api_key = api.effective_api_key
    if not api_key:
        return ""
    llm = ChatOpenAI(
        model=api.model,
        base_url=api.endpoint,
        api_key=api_key,
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


def _card_for(agent_name: str):
    """Build an agent's AgentCard from its config and the skills available to it."""
    assert _global_configuration is not None
    configuration = load_agent_configuration(agent_name, _global_configuration.agent_directories())
    all_skills = load_skills(_global_configuration.skill_directories())
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
        registry=_registry,
        on_new_context=_record_session,
        conversations=_conversations,
        on_turn_state=_set_turn_state,
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
    global _global_configuration, _session_factory, _async_engine, _task_store, _registry, _mcp_manager
    _global_configuration = GlobalConfiguration.load()

    database_path = database_file_path()
    sync_engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(sync_engine)
    _session_factory = sessionmaker(bind=sync_engine)

    exa_key = _global_configuration.exa.effective_api_key
    if exa_key:
        from exa_py import Exa
        set_exa_client(Exa(api_key=exa_key))

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


class PermissionModeRequest(BaseModel):
    mode: Literal["default", "read_only", "bypass"]


class SettingsUpdateRequest(BaseModel):
    api_key: str
    exa_api_key: str


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
async def agents():
    """List agent profiles for the UI selector."""
    assert _global_configuration is not None
    agent_data = list_agents(_global_configuration.agent_directories())
    return AgentsList(agents=[AgentInfo(id=agent["id"], name=agent["name"], title=agent.get("title", agent["name"])) for agent in agent_data])


@app.get("/agents/cards")
async def agent_cards():
    """Discovery: the full A2A AgentCard for every served agent, including their
    skills, so the UI can broadcast what each agent can do."""
    assert _registry is not None and _global_configuration is not None
    all_skills = load_skills(_global_configuration.skill_directories())
    skill_titles = {skill.identifier: skill.display_title for skill in all_skills}
    skill_enabled = {skill.identifier: skill.enabled for skill in all_skills}
    cards_by_url = {
        card.url: card.model_dump(by_alias=True, exclude_none=True, mode="json")
        for card in _registry.cards()
    }
    for card in cards_by_url.values():
        agent_name = str(card.get("name") or "")
        try:
            configuration = load_agent_configuration(agent_name, _global_configuration.agent_directories())
            card["title"] = configuration.display_name
        except Exception:
            card["title"] = agent_name
        for skill in card.get("skills", []):
            if isinstance(skill, dict):
                skill_name = str(skill.get("name") or skill.get("id") or "")
                skill["title"] = skill_titles.get(skill_name, skill_name)
                skill["enabled"] = skill_enabled.get(skill_name, True)
    return {
        "cards": list(cards_by_url.values())
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
    """Return the server user's home directory (default working directory)."""
    return {"home_directory": str(Path.home())}


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


@app.get("/settings")
async def get_settings():
    """Return the API credentials stored in ~/.harness/configuration.yaml so the
    settings dialog can pre-fill them."""
    assert _global_configuration is not None
    return {
        "api_key": _global_configuration.api.api_key,
        "exa_api_key": _global_configuration.exa.api_key,
        "sandbox_enabled": _global_configuration.sandbox.enabled,
    }


@app.post("/settings")
async def update_settings(request: SettingsUpdateRequest):
    """Persist API credentials to ~/.harness/configuration.yaml and apply them
    live: refresh the in-memory configuration, the Exa client, and drop cached
    agent runtimes so the next turn rebuilds its model client with the new key."""
    assert _global_configuration is not None
    save_api_keys(
        api_key=request.api_key,
        exa_api_key=request.exa_api_key,
    )
    _global_configuration.api.api_key = request.api_key
    _global_configuration.exa.api_key = request.exa_api_key

    exa_key = _global_configuration.exa.effective_api_key
    if exa_key:
        from exa_py import Exa
        set_exa_client(Exa(api_key=exa_key))
    else:
        set_exa_client(None)

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
async def mcp_tools(server: str = ""):
    """List every configured MCP server with its enabled flag. Enabled servers
    carry their advertised tools; disabled ones are still returned (with no tools)
    so the UI can show them greyed out rather than hiding them."""
    assert _global_configuration is not None
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
                    "running": row.id in _running_contexts,
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
    task_ids = await _task_store.task_ids_for_context(context_id)
    tasks = []
    for task_id in task_ids:
        task = await _task_store.get(task_id)
        if task is not None:
            tasks.append(task.model_dump(by_alias=True, exclude_none=True, mode="json"))
    return {"tasks": tasks}


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
    """Resolve a pending human-in-the-loop permission request."""
    future = _pending_permissions.get(request.request_id)
    if not future:
        return {"status": "unknown", "error": "No pending permission request with that identifier."}
    if future.done():
        return {"status": "stale", "error": "Permission request was already resolved."}
    future.set_result(request.decision == "allow")
    return {"status": "resolved", "decision": request.decision}


@app.post("/chat/{context_id}/abort")
async def abort_session(context_id: str):
    """Abort the running turn for a context and reject any pending permissions."""
    prefix = f"perm-{context_id}-"
    for request_id, future in list(_pending_permissions.items()):
        if request_id.startswith(prefix) and not future.done():
            future.set_result(False)
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
