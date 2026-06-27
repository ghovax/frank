import asyncio
import json
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import Column, String, Text, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sse_starlette.sse import EventSourceResponse
from watchfiles import awatch

from a2a.server.apps.jsonrpc import A2AFastAPIApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import DatabaseTaskStore

from harness.core.a2a_executor import (
    AgentRegistry,
    HarnessAgentExecutor,
    agent_rpc_path,
    build_agent_card,
)
from harness.core.configuration import (
    GlobalConfiguration,
    list_agent_route_names,
    list_agents,
    load_agent_configuration,
)
from harness.core.mcp_client import MCPClientManager
from harness.core.skills import load_skills, skills_for_agent
from harness.tools.tools import cancel_all_background_tasks, set_exa_client, set_mcp_client_manager

DATABASE_PATH = "harness.db"
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
_task_store: Optional[DatabaseTaskStore] = None
_registry: Optional[AgentRegistry] = None
_mcp_manager: Optional[MCPClientManager] = None
_executors: dict[str, HarnessAgentExecutor] = {}
_mounted_agents: set[str] = set()
_pending_permissions: dict[str, asyncio.Future] = {}
_broadcaster = Broadcaster()


def _record_session(context_id: str, agent: str, working_directory: str, first_message: str) -> None:
    assert _session_factory is not None
    database_session = _session_factory()
    try:
        if database_session.get(SessionRecord, context_id) is not None:
            return
        title = first_message.strip().split("\n", 1)[0][:80]
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


async def _watch_agents_and_skills(application: FastAPI) -> None:
    """Watch the agents and skills directories; on any change, mount newly added
    agents, refresh cards, and broadcast so connected clients refetch immediately.
    Skill files are also picked up here so new/edited skills are broadcast live."""
    assert _global_configuration is not None
    watched = _watched_a2a_paths()
    if not watched:
        return
    try:
        async for _changes in awatch(*watched):
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
    _global_configuration = GlobalConfiguration.from_yaml("configuration.yaml")

    sync_engine = create_engine(f"sqlite:///{DATABASE_PATH}")
    Base.metadata.create_all(sync_engine)
    _session_factory = sessionmaker(bind=sync_engine)

    exa_key = _global_configuration.exa.effective_api_key
    if exa_key:
        from exa_py import Exa
        set_exa_client(Exa(api_key=exa_key))

    mcp_servers = _global_configuration.mcp.enabled_servers()
    _mcp_manager = MCPClientManager(mcp_servers) if mcp_servers else None
    set_mcp_client_manager(_mcp_manager)

    _async_engine = create_async_engine(f"sqlite+aiosqlite:///{DATABASE_PATH}")
    _task_store = DatabaseTaskStore(_async_engine, create_table=True)
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


class AgentsList(BaseModel):
    agents: list[AgentInfo]


class DirectoryValidationRequest(BaseModel):
    directory: str


class PermissionRequest(BaseModel):
    request_id: str
    decision: str


class PermissionModeRequest(BaseModel):
    mode: Literal["default", "read_only", "bypass"]


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
    return AgentsList(agents=[AgentInfo(id=agent["id"], name=agent["name"]) for agent in agent_data])


@app.get("/agents/cards")
async def agent_cards():
    """Discovery: the full A2A AgentCard for every served agent, including their
    skills, so the UI can broadcast what each agent can do."""
    assert _registry is not None
    cards_by_url = {
        card.url: card.model_dump(by_alias=True, exclude_none=True, mode="json")
        for card in _registry.cards()
    }
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


@app.get("/mcp/tools")
async def mcp_tools(server: str = ""):
    """List tools exposed by configured MCP servers."""
    if _mcp_manager is None:
        return {"servers": []}
    return await _mcp_manager.list_tools(server)


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
    """Open a native folder picker on the local server machine and return a POSIX path."""
    script = 'POSIX path of (choose folder with prompt "Choose a working directory")'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return {"path": "", "cancelled": True, "error": "Folder selection timed out."}
    if result.returncode != 0:
        return {"path": "", "cancelled": True, "error": result.stderr.strip()}
    return {"path": result.stdout.strip(), "cancelled": False}


@app.get("/sessions")
async def list_sessions():
    """List recent chat sessions for the sidebar."""
    assert _session_factory is not None
    database_session = _session_factory()
    try:
        rows = database_session.query(SessionRecord).order_by(SessionRecord.created_at.desc()).limit(50).all()
        return {
            "sessions": [
                {"session_id": row.id, "agent": row.agent, "title": row.title, "created_at": row.created_at}
                for row in rows
            ]
        }
    finally:
        database_session.close()


@app.get("/sessions/{context_id}/tasks")
async def session_tasks(context_id: str):
    """All A2A tasks for a context — the main turn tasks (with history and
    artifacts) plus related sub-agent tasks — for replaying a session."""
    assert _task_store is not None and _async_engine is not None
    async with _async_engine.connect() as connection:
        result = await connection.execute(
            text("SELECT id FROM tasks WHERE context_id = :context_id"), {"context_id": context_id}
        )
        task_ids = [row[0] for row in result.fetchall()]
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
    prefix = f"perm-{context_id[:8]}-"
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
