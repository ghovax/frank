"""Agents routes."""

from __future__ import annotations
from fastapi import APIRouter, HTTPException
from frank.base.configuration import list_agents
from frank.base.skills import load_skills
import asyncio
from frank.protocol.dtos import (
    AgentConfigurationUpdateRequest,
    AgentInfo,
    AgentsList,
)
from frank.hub import state
from frank.hub.services.broadcast import _publish_broadcast
from frank.base.configuration import write_agent_markdown
from frank.hub.services.agents import _agent_configuration_for_request, _agent_configuration_payload, _apply_agent_configuration_update, _card_for, _path_scope, _record_model_selection, _reload_agent_cards

router = APIRouter()

@router.get("/agents")
async def agents(working_directory: str = ""):
    """List agent profiles for the selector, scoped to the selected folder rather than the launch directory."""
    assert state.global_configuration is not None
    if working_directory:
        directories = state.global_configuration.agent_directories_for(working_directory)
    else:
        directories = state.global_configuration.agent_directories()
    # The bundled agents are always present, and none of them is singled out as a default.
    agent_data = list_agents(directories)
    return AgentsList(agents=[
        AgentInfo(
            id=agent["id"],
            name=agent["name"],
            title=agent.get("title", agent["name"]),
            description=agent.get("description", ""),
            model=agent.get("model", ""),
        )
        for agent in agent_data
    ])


@router.get("/agents/{agent_name}/configuration")
async def agent_configuration(agent_name: str, working_directory: str = ""):
    assert state.global_configuration is not None
    try:
        return _agent_configuration_payload(agent_name, working_directory)
    except FileNotFoundError as exception:
        raise HTTPException(status_code=404, detail=str(exception)) from exception


@router.put("/agents/{agent_name}/configuration")
async def update_agent_configuration(agent_name: str, request: AgentConfigurationUpdateRequest, working_directory: str = ""):
    assert state.global_configuration is not None
    try:
        agent_markdown_path, configuration = _agent_configuration_for_request(agent_name, working_directory)
        write_agent_markdown(agent_markdown_path, _apply_agent_configuration_update(configuration, request))
        saved_configuration = _agent_configuration_payload(agent_name, working_directory)
        if saved_configuration.provider and saved_configuration.model:
            await asyncio.to_thread(_record_model_selection, f"{saved_configuration.provider}/{saved_configuration.model}")
        await state.reset_runtimes()
        _reload_agent_cards()
        _publish_broadcast({"type": "agents_changed"})
        return saved_configuration
    except FileNotFoundError as exception:
        raise HTTPException(status_code=404, detail=str(exception)) from exception


@router.get("/agents/cards")
async def agent_cards(working_directory: str = ""):
    """The full card for every served agent, including the skills the selected folder scopes."""
    assert state.global_configuration is not None
    skill_roots = (
        state.global_configuration.skill_directories_for(working_directory)
        if working_directory
        else state.global_configuration.skill_directories()
    )
    all_skills = load_skills(skill_roots)
    skill_titles = {skill.identifier: skill.display_title for skill in all_skills}
    skill_enabled = {skill.identifier: skill.enabled for skill in all_skills}
    # Cards come from the shared pool but are listed only for the agents the selected folder declares.
    allowed_agents: set[str] | None = None
    if working_directory:
        allowed_agents = {
            agent["id"]
            for agent in list_agents(state.global_configuration.agent_directories_for(working_directory))
        }
    cards: list[dict] = []
    for agent_name, existing in sorted(state.agent_cards.items()):
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


@router.get("/skills")
async def skills(working_directory: str = ""):
    """List the skills available in the selected folder, independent of any agent."""
    assert state.global_configuration is not None
    roots = (
        state.global_configuration.skill_directories_for(working_directory)
        if working_directory
        else state.global_configuration.skill_directories()
    )
    all_skills = load_skills(roots)
    home_root = state.global_configuration.home_agents_root().resolve()
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
