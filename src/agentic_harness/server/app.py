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
from agentic_harness.core.agent_configuration import (
    AgentConfiguration,
    load_agent_configuration,
    list_available_agents,
)
from agentic_harness.core.configuration import GlobalConfiguration

app = FastAPI(title="agentic-harness")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_global_config: Optional[GlobalConfiguration] = None
_sessions: dict[str, AgentOrchestrator] = {}
_session_configs: dict[str, str] = {}


class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str
    agent: str = "main"


class ChatResponse(BaseModel):
    session_id: str
    type: str
    data: dict


class AgentsList(BaseModel):
    agents: list[str]


@app.on_event("startup")
async def startup():
    global _global_config
    config_path = Path("configuration.yaml")
    if config_path.exists():
        _global_config = GlobalConfiguration.from_yaml(config_path)
    else:
        _global_config = GlobalConfiguration.from_yaml("configuration.yaml")


def _get_or_create_session(session_id: Optional[str], agent_name: str) -> tuple[str, AgentOrchestrator]:
    global _global_config
    assert _global_config is not None

    if session_id and session_id in _sessions:
        return session_id, _sessions[session_id]

    new_id = session_id or uuid.uuid4().hex[:16]
    agent_config = load_agent_configuration(agent_name, _global_config.agents_directory)
    orchestrator = AgentOrchestrator(agent_config, _global_config)
    _sessions[new_id] = orchestrator
    _session_configs[new_id] = agent_name
    return new_id, orchestrator


@app.get("/agents")
async def agents():
    global _global_config
    assert _global_config is not None
    return AgentsList(agents=list_available_agents(_global_config.agents_directory))


@app.get("/chat/{session_id}/agent")
async def get_session_agent(session_id: str):
    agent_name = _session_configs.get(session_id, "main")
    return {"agent": agent_name}


@app.post("/chat/{session_id}/switch")
async def switch_agent(session_id: str, agent: str):
    global _global_config
    assert _global_config is not None

    if session_id in _sessions:
        del _sessions[session_id]

    agent_config = load_agent_configuration(agent, _global_config.agents_directory)
    orchestrator = AgentOrchestrator(agent_config, _global_config)
    _sessions[session_id] = orchestrator
    _session_configs[session_id] = agent
    return {"agent": agent}


@app.post("/chat")
async def chat(req: ChatRequest):
    session_id, orchestrator = _get_or_create_session(req.session_id, req.agent)

    async def event_generator():
        yield {"event": "session", "data": json.dumps({"session_id": session_id})}
        async for event in orchestrator.stream(req.message):
            yield {"event": event.type.value, "data": event.to_json()}

    return EventSourceResponse(event_generator())


def create_server_app() -> FastAPI:
    return app


def run_server(host: str = "127.0.0.1", port: int = 8822):
    import uvicorn
    uvicorn.run(app, host=host, port=port)
