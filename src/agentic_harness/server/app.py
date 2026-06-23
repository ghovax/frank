import asyncio
import json
import uuid
from datetime import datetime, timezone
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
from agentic_harness.server.models import (
    Session as DBSession,
    Message,
    ExecutionEvent,
    Orchestration,
    create_database,
)
from sqlalchemy.orm import Session as DBSessionType

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
_database_engine = None
_database_session_factory = None


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str
    agent: str = "main"


class PermissionRequest(BaseModel):
    request_id: str
    decision: str


class AgentsList(BaseModel):
    agents: list[str]


def get_database() -> DBSessionType:
    return _database_session_factory()


@app.on_event("startup")
async def startup():
    global _global_configuration, _database_engine, _database_session_factory
    configuration_path = Path("configuration.yaml")
    if configuration_path.exists():
        _global_configuration = GlobalConfiguration.from_yaml(configuration_path)
    else:
        _global_configuration = GlobalConfiguration.from_yaml("configuration.yaml")
    _database_engine, _database_session_factory = create_database()


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

    def record_event(event_type: str, data: dict) -> None:
        database_session = get_database()
        try:
            database_session.add(ExecutionEvent(
                session_id=new_identifier,
                type=event_type,
                data=json.dumps(data),
                timestamp=datetime.now(timezone.utc).isoformat(),
            ))
            database_session.commit()
        finally:
            database_session.close()

    def record_message(role: str, content: str, tool_call_id: str) -> None:
        database_session = get_database()
        try:
            database_session.add(Message(
                session_id=new_identifier,
                role=role,
                content=content,
                tool_call_id=tool_call_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
            ))
            database_session.commit()
        finally:
            database_session.close()

    def record_orchestration(orchestration_id: str, thread_id: str, steps: list, results: list) -> None:
        database_session = get_database()
        try:
            database_session.add(Orchestration(
                id=orchestration_id,
                session_id=new_identifier,
                thread_id=thread_id,
                steps=json.dumps(steps),
                results=json.dumps(results),
                created_at=datetime.now(timezone.utc).isoformat(),
            ))
            database_session.commit()
        finally:
            database_session.close()

    orchestrator = AgentOrchestrator(
        agent_configuration=agent_configuration,
        global_configuration=_global_configuration,
        pending_permissions=_pending_permissions,
        on_record_event=record_event,
        on_record_message=record_message,
        on_record_orchestration=record_orchestration,
        session_id=new_identifier,
    )
    _sessions[new_identifier] = orchestrator
    _session_configurations[new_identifier] = agent_name

    database_session = get_database()
    try:
        database_session.add(DBSession(
            id=new_identifier,
            agent=agent_name,
            created_at=datetime.now(timezone.utc).isoformat(),
        ))
        database_session.commit()
    finally:
        database_session.close()

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
        session_id=session_id,
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
    orchestrator = _sessions.get(session_id)
    if not orchestrator:
        return {"status": "not_found", "session_id": session_id}

    session_prefix = f"perm-{session_id[:8]}-"
    for request_id, future in list(_pending_permissions.items()):
        if request_id.startswith(session_prefix) and not future.done():
            future.set_result(False)

    orchestrator.abort()

    database_session = get_database()
    try:
        database_session.add(ExecutionEvent(
            session_id=session_id,
            type="session_cancelled",
            data=json.dumps({"stop_reason": "cancelled"}),
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
        database_session.commit()
    finally:
        database_session.close()

    return {"status": "aborted", "session_id": session_id}


@app.get("/sessions/{session_id}/history")
async def get_execution_history(session_id: str):
    database_session = get_database()
    try:
        rows = database_session.query(ExecutionEvent).filter(
            ExecutionEvent.session_id == session_id
        ).order_by(ExecutionEvent.id).all()
        return {
            "history": [
                {"type": row.type, **json.loads(row.data), "timestamp": row.timestamp}
                for row in rows
            ]
        }
    finally:
        database_session.close()


@app.get("/sessions/{session_id}/orchestrations")
async def list_orchestrations(session_id: str):
    database_session = get_database()
    try:
        rows = database_session.query(Orchestration).filter(
            Orchestration.session_id == session_id
        ).order_by(Orchestration.created_at).all()
        return {
            "orchestrations": [
                {
                    "orchestration_id": row.id,
                    "thread_id": row.thread_id,
                    "steps": json.loads(row.steps),
                    "results": json.loads(row.results),
                    "created_at": row.created_at,
                }
                for row in rows
            ]
        }
    finally:
        database_session.close()


@app.get("/sessions/{session_id}/orchestrations/{thread_id}")
async def get_orchestration_detail(session_id: str, thread_id: str):
    orchestrator = _sessions.get(session_id)
    if not orchestrator:
        return {"error": "session not found"}
    checkpoints = orchestrator.get_orchestration_checkpoints(thread_id)
    return {"thread_id": thread_id, "checkpoints": checkpoints}


@app.get("/sessions/{session_id}/conversation")
async def get_conversation(session_id: str):
    database_session = get_database()
    try:
        rows = database_session.query(Message).filter(
            Message.session_id == session_id
        ).order_by(Message.id).all()
        return {
            "messages": [
                {
                    "role": row.role,
                    "content": row.content,
                    "tool_call_id": row.tool_call_id,
                    "timestamp": row.timestamp,
                }
                for row in rows
            ]
        }
    finally:
        database_session.close()


@app.post("/chat")
async def chat(request: ChatRequest):
    session_id, orchestrator = _get_or_create_session(request.session_id, request.agent)

    async def event_generator():
        try:
            yield {
                "event": "session",
                "data": json.dumps({"type": "session", "session_id": session_id}),
            }
            async for event in orchestrator.stream(request.message):
                yield {"event": event.type.value, "data": event.to_json()}
        except GeneratorExit:
            pass

    return EventSourceResponse(event_generator())


def run_server(host: str = "127.0.0.1", port: int = 8822):
    import uvicorn

    uvicorn.run(app, host=host, port=port)
