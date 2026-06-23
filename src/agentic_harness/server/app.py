import asyncio
import json
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from agentic_harness.core.agent import AgentOrchestrator, StreamEvent
from agentic_harness.core.configuration import (
    GlobalConfiguration,
    load_agent_configuration,
    list_available_agents,
)

app = FastAPI(title="agentic-harness")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_global_configuration: Optional[GlobalConfiguration] = None
_sessions: dict[str, AgentOrchestrator] = {}
_session_configurations: dict[str, str] = {}
_pending_permissions: dict[str, asyncio.Future] = {}
_abort_events: dict[str, asyncio.Event] = {}


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str
    agent: str = "main"


class PermissionRequest(BaseModel):
    request_id: str
    decision: str


class AgentsList(BaseModel):
    agents: list[str]


@app.on_event("startup")
async def startup():
    global _global_configuration
    configuration_path = Path("configuration.yaml")
    if configuration_path.exists():
        _global_configuration = GlobalConfiguration.from_yaml(configuration_path)
    else:
        _global_configuration = GlobalConfiguration.from_yaml("configuration.yaml")


def _get_or_create_session(
    session_id: Optional[str], agent_name: str
) -> tuple[str, AgentOrchestrator]:
    global _global_configuration
    assert _global_configuration is not None

    if session_id and session_id in _sessions:
        return session_id, _sessions[session_id]

    new_identifier = session_id or uuid.uuid4().hex[:16]
    agent_configuration = load_agent_configuration(
        agent_name, _global_configuration.agents_directory
    )
    orchestrator = AgentOrchestrator(
        agent_configuration=agent_configuration,
        global_configuration=_global_configuration,
        pending_permissions=_pending_permissions,
    )
    _sessions[new_identifier] = orchestrator
    _session_configurations[new_identifier] = agent_name
    return new_identifier, orchestrator


@app.get("/agents")
async def agents():
    global _global_configuration
    assert _global_configuration is not None
    return AgentsList(
        agents=list_available_agents(_global_configuration.agents_directory)
    )


@app.post("/chat/{session_id}/switch")
async def switch_agent(session_id: str, agent: str):
    global _global_configuration
    assert _global_configuration is not None

    if session_id in _sessions:
        del _sessions[session_id]

    agent_configuration = load_agent_configuration(
        agent, _global_configuration.agents_directory
    )
    orchestrator = AgentOrchestrator(
        agent_configuration=agent_configuration,
        global_configuration=_global_configuration,
        pending_permissions=_pending_permissions,
    )
    _sessions[session_id] = orchestrator
    _session_configurations[session_id] = agent
    return {"agent": agent}


@app.post("/chat/{session_id}/permission")
async def resolve_permission(session_id: str, request: PermissionRequest):
    future = _pending_permissions.get(request.request_id)
    if not future:
        return {"status": "unknown", "error": "No pending permission request with that identifier."}
    allowed = request.decision == "allow"
    future.set_result(allowed)
    return {"status": "resolved", "decision": request.decision}


@app.get("/chat/{session_id}/status")
async def session_status(session_id: str):
    agent_name = _session_configurations.get(session_id, "unknown")
    has_session = session_id in _sessions
    return {
        "session_id": session_id,
        "agent": agent_name,
        "active": has_session,
    }


@app.post("/chat/{session_id}/abort")
async def abort_session(session_id: str):
    abort_event = _abort_events.get(session_id)
    if abort_event:
        abort_event.set()
    return {"status": "aborted", "session_id": session_id}


@app.get("/sessions/{session_id}/history")
async def get_execution_history(session_id: str):
    orchestrator = _sessions.get(session_id)
    if not orchestrator:
        return {"error": "session not found"}
    return {"history": orchestrator.get_execution_history()}


@app.get("/sessions/{session_id}/orchestrations")
async def list_orchestrations(session_id: str):
    orchestrator = _sessions.get(session_id)
    if not orchestrator:
        return {"error": "session not found"}
    history = orchestrator.get_execution_history()
    orchestrations = [entry for entry in history if entry.get("type") == "orchestration"]
    return {"orchestrations": orchestrations}


@app.get("/sessions/{session_id}/orchestrations/{thread_id}")
async def get_orchestration_detail(session_id: str, thread_id: str):
    orchestrator = _sessions.get(session_id)
    if not orchestrator:
        return {"error": "session not found"}
    checkpoints = orchestrator.get_orchestration_checkpoints(thread_id)
    return {"thread_id": thread_id, "checkpoints": checkpoints}


@app.post("/chat")
async def chat(request: ChatRequest):
    session_id, orchestrator = _get_or_create_session(request.session_id, request.agent)

    abort_event = asyncio.Event()
    _abort_events[session_id] = abort_event

    async def event_generator():
        try:
            yield {
                "event": "session",
                "data": json.dumps({"type": "session", "session_id": session_id}),
            }
            async for event in orchestrator.stream(request.message):
                if abort_event.is_set():
                    yield {
                        "event": "done",
                        "data": json.dumps({"type": "done", "stop_reason": "interrupted", "text": "interrupted by user", "session_id": session_id}),
                    }
                    return
                yield {"event": event.type.value, "data": event.to_json()}
        except GeneratorExit:
            pass
        finally:
            _abort_events.pop(session_id, None)

    return EventSourceResponse(event_generator())


def run_server(host: str = "127.0.0.1", port: int = 8822):
    import uvicorn

    uvicorn.run(app, host=host, port=port)
