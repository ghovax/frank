"""Remote-agent routes: peers on other hosts, registered by their A2A card.

The home ``~/.agents/remote-agents.json`` is the source of truth; these endpoints let the
app read the registered agents (with live health resolved from the outbound client
manager) and edit that file. A write reloads the manager immediately (the file watcher
also reloads, so the two are idempotent)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from daisy.workspace import state
from daisy.workspace.services.broadcast import _publish_broadcast
from daisy.workspace.brokers.remote_agents import _reload_remote_agents

router = APIRouter()


class RemoteAgentAuthInput(BaseModel):
    type: str = "none"
    token: str = ""
    header: str = "Authorization"
    schemePrefix: str = "Bearer"
    tokenUrl: str = ""
    clientId: str = ""
    clientSecret: str = ""
    scopes: list[str] = []


class RemoteAgentInput(BaseModel):
    name: str
    cardUrl: str
    enabled: bool = True
    auth: RemoteAgentAuthInput = RemoteAgentAuthInput()
    cardTtlSeconds: int = 3600
    allowedHosts: list[str] = []
    allowPrivate: bool = False
    allowedProfiles: list[str] = []


def _home_remote_agents_path() -> Path:
    assert state.global_configuration is not None
    root = state.global_configuration.home_agents_root()
    root.mkdir(parents=True, exist_ok=True)
    return root / "remote-agents.json"


def _read_file() -> dict:
    path = _home_remote_agents_path()
    if not path.exists():
        return {"agents": {}}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"agents": {}}
    if not isinstance(data.get("agents"), dict):
        data["agents"] = {}
    return data


def _write_file(data: dict) -> None:
    _home_remote_agents_path().write_text(json.dumps(data, indent=2))


def _entry_from_input(payload: RemoteAgentInput) -> dict:
    """The ``remote-agents.json`` entry (snake_case, the loader's schema) for one agent.
    Auth fields that are empty are dropped so the file stays tidy; secrets may be written
    as ``${ENV_VAR}`` references, which the loader expands."""
    auth = {"type": payload.auth.type}
    if payload.auth.type in {"bearer", "api_key"}:
        auth["token"] = payload.auth.token
        auth["header"] = payload.auth.header
        auth["scheme_prefix"] = payload.auth.schemePrefix
    elif payload.auth.type == "oauth2":
        auth["token_url"] = payload.auth.tokenUrl
        auth["client_id"] = payload.auth.clientId
        auth["client_secret"] = payload.auth.clientSecret
        auth["scopes"] = payload.auth.scopes
    return {
        "card_url": payload.cardUrl,
        "enabled": payload.enabled,
        "auth": auth,
        "card_ttl_seconds": payload.cardTtlSeconds,
        "allowed_hosts": payload.allowedHosts,
        "allow_private": payload.allowPrivate,
        "allowed_profiles": payload.allowedProfiles,
    }


@router.get("/remote-agents")
async def list_remote_agents():
    """The registered external agents with their config (never secrets) and live health."""
    assert state.global_configuration is not None
    configuration = state.global_configuration.remote_agents
    manager = state.remote_agent_manager
    agents = []
    for name, agent in configuration.agents.items():
        health = manager.health(name) if manager is not None else {"health": "unconfigured", "error": ""}
        card = manager.card(name) if manager is not None else None
        agents.append({
            "name": name,
            "cardUrl": agent.card_url,
            "enabled": agent.enabled,
            "authType": agent.auth.type,
            "allowedProfiles": agent.allowed_profiles,
            "allowedHosts": agent.allowed_hosts,
            "allowPrivate": agent.allow_private,
            "cardTtlSeconds": agent.card_ttl_seconds,
            "health": health["health"],
            "error": health["error"],
            "resolvedName": card.name if card is not None else "",
            "resolvedDescription": (card.description if card is not None else "") or "",
            "skills": [skill.name for skill in (card.skills or [])] if card is not None else [],
        })
    agents.sort(key=lambda entry: entry["name"])
    return {"agents": agents}


@router.put("/remote-agents/{name}")
async def upsert_remote_agent(name: str, payload: RemoteAgentInput):
    """Add or replace one remote agent, then reload the manager so it takes effect."""
    if not payload.cardUrl:
        raise HTTPException(status_code=400, detail="cardUrl is required.")
    data = await asyncio.to_thread(_read_file)
    data["agents"][name] = _entry_from_input(payload)
    await asyncio.to_thread(_write_file, data)
    await _reload_remote_agents()
    return {"ok": True}


@router.delete("/remote-agents/{name}")
async def delete_remote_agent(name: str):
    data = await asyncio.to_thread(_read_file)
    if name not in data["agents"]:
        raise HTTPException(status_code=404, detail="No such remote agent.")
    del data["agents"][name]
    await asyncio.to_thread(_write_file, data)
    await _reload_remote_agents()
    return {"ok": True}


@router.post("/remote-agents/{name}/refresh")
async def refresh_remote_agent(name: str):
    """Force a fresh card resolution for one agent and return its new health."""
    manager = state.remote_agent_manager
    if manager is None or not manager.is_remote(name):
        raise HTTPException(status_code=404, detail="No such remote agent.")
    await manager.refresh(name)
    _publish_broadcast({"type": "remote_agents_changed"})
    return {"name": name, **manager.health(name)}
