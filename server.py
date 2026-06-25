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
from sqlalchemy import Column, Integer, String, Text, create_engine, func, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from langchain_openai import ChatOpenAI

from harness.core.agent import AgentOrchestrator, StreamEvent
from harness.tools.tools import set_exa_client
from harness.core.configuration import (
    GlobalConfiguration,
    load_agent_configuration,
    list_agents_with_labels,
)

Base = declarative_base()


class SessionRecord(Base):
    __tablename__ = "sessions"

    id = Column(String, primary_key=True)
    agent = Column(String, nullable=False)
    working_directory = Column(Text, default="")
    title = Column(Text, default="")
    created_at = Column(String, nullable=False)


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String, nullable=False, index=True)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    tool_call_id = Column(String, default="")
    timestamp = Column(String, nullable=False)


class ExecutionEvent(Base):
    __tablename__ = "execution_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String, nullable=False, index=True)
    type = Column(String, nullable=False)
    data = Column(Text, nullable=False)
    timestamp = Column(String, nullable=False)


class StreamEventRecord(Base):
    __tablename__ = "stream_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String, nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    type = Column(String, nullable=False)
    data = Column(Text, nullable=False)
    timestamp = Column(String, nullable=False)


class Orchestration(Base):
    __tablename__ = "orchestrations"

    id = Column(String, primary_key=True)
    session_id = Column(String, nullable=False, index=True)
    thread_id = Column(String, nullable=False)
    steps = Column(Text, nullable=False)
    results = Column(Text, nullable=False)
    created_at = Column(String, nullable=False)


def _ensure_schema(engine) -> None:
    """Add columns that exist on the models but are missing from an older
    database. ``create_all`` only creates absent tables — it never alters
    existing ones — so a database created before a column was introduced (e.g.
    ``sessions.title``) would otherwise raise OperationalError on every query
    that references it, breaking ``/sessions`` and aborting the chat stream.
    """
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing_columns = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing_columns:
                continue
            column_type = column.type.compile(engine.dialect)
            with engine.begin() as connection:
                connection.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}')
                )


def create_database(database_path: str = "harness.db") -> tuple:
    engine = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    _ensure_schema(engine)
    session_factory = sessionmaker(bind=engine)
    return engine, session_factory


app = FastAPI(title="harness")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

_global_configuration: Optional[GlobalConfiguration] = None
_sessions: dict[str, AgentOrchestrator] = {}
_session_configurations: dict[str, str] = {}
_pending_permissions: dict[str, asyncio.Future] = {}
_database_engine = None
_database_session_factory = None
_saved_sessions: set[str] = set()
_titles_generated: set[str] = set()


def generate_session_title(user_message: str) -> str:
    try:
        llm = ChatOpenAI(
            model=_global_configuration.api.model,
            base_url=_global_configuration.api.endpoint,
            api_key=_global_configuration.api.api_key,
            temperature=0,
        ).bind(response_format={"type": "json_object"})
        response = llm.invoke([
            {"role": "system", "content": "Generate a concise title (at most 6 words) for a chat session based on the user's first message. Respond with a JSON object containing a 'title' field."},
            {"role": "user", "content": user_message},
        ])
        data = json.loads(response.content)
        return data.get("title", "")
    except Exception:
        return ""


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str
    agent: str = "main"
    working_directory: Optional[str] = None


class PermissionRequest(BaseModel):
    request_id: str
    decision: str


class AgentInfo(BaseModel):
    name: str
    label: str


class AgentsList(BaseModel):
    agents: list[AgentInfo]


class DirectoryUpdateRequest(BaseModel):
    directory: str


class BypassPermissionsRequest(BaseModel):
    bypass: bool


def get_database() -> sessionmaker:
    return _database_session_factory()


def record_stream_event(session_id: str, event_type: str, data: dict, sequence: int) -> None:
    database_session = get_database()
    try:
        database_session.add(StreamEventRecord(
            session_id=session_id,
            sequence=sequence,
            type=event_type,
            data=json.dumps(data),
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
        database_session.commit()
    finally:
        database_session.close()


@app.on_event("startup")
async def startup():
    global _global_configuration, _database_engine, _database_session_factory
    configuration_path = Path("configuration.yaml")
    if configuration_path.exists():
        _global_configuration = GlobalConfiguration.from_yaml(configuration_path)
    else:
        _global_configuration = GlobalConfiguration.from_yaml("configuration.yaml")
    _database_engine, _database_session_factory = create_database()

    exa_key = _global_configuration.exa.effective_api_key
    if exa_key:
        from exa_py import Exa
        set_exa_client(Exa(api_key=exa_key))


def _get_or_create_session(
    session_id: Optional[str], agent_name: str, working_directory: Optional[str] = None
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
        working_directory=working_directory or "",
    )
    _sessions[new_identifier] = orchestrator
    _session_configurations[new_identifier] = agent_name
    return new_identifier, orchestrator


@app.get("/agents")
async def agents():
    global _global_configuration
    assert _global_configuration is not None
    agent_data = list_agents_with_labels(_global_configuration.agents_directory)
    return AgentsList(
        agents=[AgentInfo(name=a["name"], label=a["label"]) for a in agent_data]
    )


@app.get("/home")
async def home_directory():
    from pathlib import Path
    return {"home_directory": str(Path.home())}


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
    working_directory_path = ""
    if has_session:
        working_directory_path = _sessions[session_id].working_directory
    return {
        "session_id": session_id,
        "agent": agent_name,
        "active": has_session,
        "working_directory": working_directory_path,
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


@app.patch("/chat/{session_id}/directory")
async def update_directory(session_id: str, request: DirectoryUpdateRequest):
    orchestrator = _sessions.get(session_id)
    if not orchestrator:
        return {"status": "not_found"}
    orchestrator._working_directory = request.directory
    database_session = get_database()
    try:
        database_session.query(SessionRecord).filter(
            SessionRecord.id == session_id
        ).update({"working_directory": request.directory})
        database_session.commit()
    finally:
        database_session.close()
    return {"status": "updated", "directory": request.directory}


@app.post("/chat/{session_id}/permissions/bypass")
async def toggle_bypass_permissions(session_id: str, request: BypassPermissionsRequest):
    orchestrator = _sessions.get(session_id)
    if not orchestrator:
        return {"status": "not_found"}
    orchestrator.set_bypass_permissions(request.bypass)
    return {"status": "updated", "bypass": request.bypass}


@app.get("/sessions")
async def list_sessions():
    database_session = get_database()
    try:
        rows = database_session.query(SessionRecord).order_by(
            SessionRecord.created_at.desc()
        ).limit(50).all()
        return {
            "sessions": [
                {
                    "session_id": row.id,
                    "agent": row.agent,
                    "title": row.title,
                    "created_at": row.created_at,
                }
                for row in rows
            ]
        }
    finally:
        database_session.close()


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
        rows = database_session.query(StreamEventRecord).filter(
            StreamEventRecord.session_id == session_id
        ).order_by(StreamEventRecord.sequence).all()
        return {
            "events": [
                {"type": row.type, **json.loads(row.data), "timestamp": row.timestamp}
                for row in rows
            ]
        }
    finally:
        database_session.close()


@app.post("/chat")
async def chat(request: ChatRequest):
    session_id, orchestrator = _get_or_create_session(
        request.session_id, request.agent, request.working_directory,
    )

    async def event_generator():
        pending_text = ""
        pending_thinking = ""
        # Keyed by "orchestration_id::step_id" so steps from different
        # orchestrations never share a buffer.
        pending_agent_buffers: dict[str, dict[str, str]] = {}
        database_session = get_database()
        try:
            row = database_session.query(
                func.max(StreamEventRecord.sequence)
            ).filter(
                StreamEventRecord.session_id == session_id
            ).first()
            sequence_number = row[0] if row and row[0] is not None else 0
        finally:
            database_session.close()

        def flush_main():
            nonlocal pending_text, pending_thinking, sequence_number
            if pending_text:
                sequence_number += 1
                record_stream_event(session_id, "text_chunk", {"text": pending_text}, sequence_number)
                pending_text = ""
            if pending_thinking:
                sequence_number += 1
                record_stream_event(session_id, "thinking", {"text": pending_thinking}, sequence_number)
                pending_thinking = ""

        def flush_agent(orchestration_id: str, step_id: str):
            nonlocal sequence_number
            buffer = pending_agent_buffers.pop(f"{orchestration_id}::{step_id}", None)
            if buffer:
                common = {"step_id": step_id, "orchestration_id": orchestration_id}
                if buffer.get("text"):
                    sequence_number += 1
                    record_stream_event(session_id, "agent_text_chunk", {"text": buffer["text"], **common}, sequence_number)
                if buffer.get("thinking"):
                    sequence_number += 1
                    record_stream_event(session_id, "agent_thinking", {"text": buffer["thinking"], **common}, sequence_number)

        try:
            yield {
                "event": "session",
                "data": json.dumps({"type": "session", "session_id": session_id}),
            }

            sequence_number += 1
            record_stream_event(session_id, "user", {"text": request.message}, sequence_number)

            if session_id not in _saved_sessions:
                _saved_sessions.add(session_id)
                database_session = get_database()
                try:
                    database_session.add(SessionRecord(
                        id=session_id,
                        agent=request.agent,
                        working_directory=request.working_directory or "",
                        created_at=datetime.now(timezone.utc).isoformat(),
                    ))
                    database_session.commit()
                finally:
                    database_session.close()

            async for event in orchestrator.stream(request.message):
                if event.type == StreamEvent.Type.TEXT_CHUNK:
                    pending_text += event.data.get("text", "")
                elif event.type == StreamEvent.Type.THINKING:
                    pending_thinking += event.data.get("text", "")
                elif event.type == StreamEvent.Type.STATUS:
                    flush_main()
                    sequence_number += 1
                    record_stream_event(session_id, "status", event.data, sequence_number)
                elif event.type == StreamEvent.Type.TOOL_CALL:
                    flush_main()
                    sequence_number += 1
                    record_stream_event(session_id, "tool_call", event.data, sequence_number)
                elif event.type == StreamEvent.Type.TOOL_RESULT:
                    flush_main()
                    sequence_number += 1
                    record_stream_event(session_id, "tool_result", event.data, sequence_number)
                elif event.type == StreamEvent.Type.PERMISSION_REQUEST:
                    sequence_number += 1
                    record_stream_event(session_id, "permission_request", event.data, sequence_number)
                elif event.type == StreamEvent.Type.ERROR:
                    sequence_number += 1
                    record_stream_event(session_id, "error", event.data, sequence_number)
                elif event.type == StreamEvent.Type.DENIED_INJECTION:
                    sequence_number += 1
                    record_stream_event(session_id, "denied_injection", event.data, sequence_number)
                elif event.type == StreamEvent.Type.BACKGROUND_STARTED:
                    sequence_number += 1
                    record_stream_event(session_id, "background_started", event.data, sequence_number)
                elif event.type == StreamEvent.Type.TASKS_UPDATED:
                    sequence_number += 1
                    record_stream_event(session_id, "tasks_updated", event.data, sequence_number)
                elif event.type == StreamEvent.Type.DONE:
                    flush_main()
                    if session_id in _saved_sessions and session_id not in _titles_generated:
                        _titles_generated.add(session_id)
                        title = generate_session_title(request.message)
                        if title:
                            database_session = get_database()
                            try:
                                session_row = database_session.query(SessionRecord).filter(SessionRecord.id == session_id).first()
                                if session_row:
                                    session_row.title = title
                                    database_session.commit()
                            finally:
                                database_session.close()
                elif event.type == StreamEvent.Type.ORCHESTRATION_STARTED:
                    flush_main()
                    sequence_number += 1
                    record_stream_event(session_id, "orchestration_started", event.data, sequence_number)
                elif event.type == StreamEvent.Type.AGENT_TEXT_CHUNK:
                    key = f"{event.data.get('orchestration_id', '')}::{event.data.get('step_id', '')}"
                    if key not in pending_agent_buffers:
                        pending_agent_buffers[key] = {"text": "", "thinking": ""}
                    pending_agent_buffers[key]["text"] += event.data.get("text", "")
                elif event.type == StreamEvent.Type.AGENT_THINKING:
                    key = f"{event.data.get('orchestration_id', '')}::{event.data.get('step_id', '')}"
                    if key not in pending_agent_buffers:
                        pending_agent_buffers[key] = {"text": "", "thinking": ""}
                    pending_agent_buffers[key]["thinking"] += event.data.get("text", "")
                elif event.type == StreamEvent.Type.AGENT_TOOL_CALL:
                    flush_agent(event.data.get("orchestration_id", ""), event.data.get("step_id", ""))
                    sequence_number += 1
                    record_stream_event(session_id, "agent_tool_call", event.data, sequence_number)
                elif event.type == StreamEvent.Type.AGENT_DONE:
                    flush_agent(event.data.get("orchestration_id", ""), event.data.get("step_id", ""))
                    sequence_number += 1
                    record_stream_event(session_id, "agent_done", event.data, sequence_number)

                yield {"event": event.type.value, "data": event.to_json()}
        except GeneratorExit:
            pass
        except Exception as exception:
            # Surface the failure to the client as a normal error event and a
            # terminal done event, so the EventSource closes gracefully instead
            # of the browser seeing ERR_INCOMPLETE_CHUNKED_ENCODING.
            yield {
                "event": "error",
                "data": json.dumps({"type": "error", "message": f"Server error: {exception}"}),
            }
            yield {
                "event": "done",
                "data": json.dumps({"type": "done", "stop_reason": "error"}),
            }

    return EventSourceResponse(event_generator())


def run_server(host: str = "127.0.0.1", port: int = 8822):
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8822)
