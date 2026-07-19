"""Agents routes (split from harness.server.runtime)."""
from fastapi import APIRouter
from fastapi import HTTPException
from harness.core.configuration import list_agents
from harness.core.skills import load_skills
import asyncio
from harness.server.models import (
    AgentConfigurationUpdateRequest,
    AgentInfo,
    AgentsList,
)
from harness.server import runtime as _app
from harness.server.runtime import (
    AGENT_CARD_PATH,
    _agent_configuration_for_request,
    _agent_configuration_payload,
    _apply_agent_configuration_update,
    _card_for,
    _ensure_agents_for,
    _executors,
    _load_agent_sidecar,
    _path_scope,
    _publish_broadcast,
    _record_model_selection,
    _reload_agent_cards,
    _save_agent_sidecar,
)

router = APIRouter()

@router.get("/agents")
async def agents(working_directory: str = ""):
    """List agent profiles for the UI selector, scoped to the selected folder:
    the home globals plus that folder's own ``.agents/agents`` (deduped), never
    the directory the server was launched in. Passing ``working_directory`` is
    what makes the list track the chosen folder."""
    assert _app._global_configuration is not None
    if working_directory:
        _ensure_agents_for(working_directory)
        directories = _app._global_configuration.agent_directories_for(working_directory)
    else:
        directories = _app._global_configuration.agent_directories()
    agent_data = list_agents(directories)
    # The bundled agents are always present, so a folder with no ``.agents`` of
    # its own still sees the shipped profiles. The configured default agent is
    # only offered as the selection fallback when it is actually available in
    # this folder's resolved set.
    available_ids = {agent["id"] for agent in agent_data}
    default_agent = _app._global_configuration.default_agent if _app._global_configuration.default_agent in available_ids else (agent_data[0]["id"] if agent_data else "")
    return AgentsList(agents=[AgentInfo(id=agent["id"], name=agent["name"], title=agent.get("title", agent["name"]), description=agent.get("description", ""), model=agent.get("model", "")) for agent in agent_data], defaultAgent=default_agent)


@router.get("/agents/{agent_name}/configuration")
async def agent_configuration(agent_name: str, working_directory: str = ""):
    assert _app._global_configuration is not None
    try:
        if working_directory:
            _ensure_agents_for(working_directory)
        return _agent_configuration_payload(agent_name, working_directory)
    except FileNotFoundError as exception:
        raise HTTPException(status_code=404, detail=str(exception)) from exception


@router.put("/agents/{agent_name}/configuration")
async def update_agent_configuration(agent_name: str, request: AgentConfigurationUpdateRequest, working_directory: str = ""):
    assert _app._global_configuration is not None
    try:
        if working_directory:
            _ensure_agents_for(working_directory)
        agent_markdown_path, _configuration_data = _agent_configuration_for_request(agent_name, working_directory)
        sidecar = _load_agent_sidecar(agent_markdown_path)
        _save_agent_sidecar(agent_markdown_path, _apply_agent_configuration_update(sidecar, request))
        saved_configuration = _agent_configuration_payload(agent_name, working_directory)
        if saved_configuration.provider and saved_configuration.model:
            await asyncio.to_thread(_record_model_selection, f"{saved_configuration.provider}/{saved_configuration.model}")
        if agent_name in _executors:
            _executors[agent_name].reset_runtimes()
        _reload_agent_cards()
        _publish_broadcast({"type": "agents_changed"})
        return saved_configuration
    except FileNotFoundError as exception:
        raise HTTPException(status_code=404, detail=str(exception)) from exception


@router.get("/agents/cards")
async def agent_cards(working_directory: str = ""):
    """Discovery: the full A2A AgentCard for every served agent, including their
    skills, so the UI can broadcast what each agent can do.

    Skills are scoped to ``working_directory`` when given: the home globals plus
    that path's own ``.agents`` skills (deduped), and crucially *not* the skills of
    the directory the server happens to have been launched in. The UI passes the
    selected project path so the advertised skills match what a session there can
    actually find, refreshing whenever the user picks a different folder."""
    assert _app._registry is not None and _app._global_configuration is not None
    skill_roots = (
        _app._global_configuration.skill_directories_for(working_directory)
        if working_directory
        else _app._global_configuration.skill_directories()
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
            for agent in list_agents(_app._global_configuration.agent_directories_for(working_directory))
        }
    cards: list[dict] = []
    for existing in _app._registry.cards():
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


@router.get("/skills")
async def skills(working_directory: str = ""):
    """List the skills available in the selected folder — home globals plus that
    folder's own ``.agents/skills`` (deduped), never the launch directory. This is
    independent of any agent, so the UI can show a folder's skills even when it has
    no agents. Disabled skills are returned (flagged) so the UI greys them out."""
    assert _app._global_configuration is not None
    roots = (
        _app._global_configuration.skill_directories_for(working_directory)
        if working_directory
        else _app._global_configuration.skill_directories()
    )
    all_skills = load_skills(roots)
    home_root = _app._global_configuration.home_agents_root().resolve()
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


@router.get(AGENT_CARD_PATH)
async def default_agent_card():
    """Serve the default agent's card at the well-known path for spec compliance."""
    assert _app._registry is not None and _app._global_configuration is not None
    card = _app._registry.card(_app._global_configuration.default_agent) or (
        _app._registry.cards()[0] if _app._registry.cards() else None
    )
    if card is None:
        return {}
    return card.model_dump(by_alias=True, exclude_none=True, mode="json")
