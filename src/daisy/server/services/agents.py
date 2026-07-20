"""Agent domain: card building, per-request configuration resolution, sidecar
read/write, configuration updates, and model-selection history."""

from __future__ import annotations
from daisy.server.models import AgentConfigurationUpdateRequest

from datetime import datetime
from datetime import timezone
from daisy.core.a2a_executor import build_agent_card
from daisy.core.configuration import AgentSidecar
from daisy.core.configuration import agent_configuration_path
from daisy.core.configuration import list_agent_route_names
from daisy.core.configuration import load_agent_configuration
from daisy.core.models import find_model
from daisy.core.models import provider_and_suffix
from daisy.core.skills import load_skills
from daisy.core.skills import skills_for_agent
from daisy.core.sqlite_lock import sqlite_write_lock
from daisy.server.models import AgentBashConfigurationResponse
from daisy.server.models import AgentConfigurationResponse
from daisy.server.models import AgentSpawnConfigurationResponse
from pathlib import Path
from typing import Any
import daisy.core.configuration as _configuration
import json
from daisy.server import state
from daisy.server.database import ModelHistoryRecord


PUBLIC_BASE_URL = "http://localhost:8822"


AGENT_CARD_PATH = "/.well-known/agent-card.json"


def _path_scope(path_value: str, home_root: Path) -> str:
    """Whether a discovered file is ``global`` (under ``~/.agents``) or
    ``project`` (the selected folder's own ``.agents``)."""
    try:
        return "global" if Path(path_value).resolve().is_relative_to(home_root) else "project"
    except Exception:
        return "global"


def _record_model_selection(model_identifier: str) -> None:
    """Record a model selection in the history (upserting by id), mirroring the
    project-history list. Catalog models use their display label; typed model ids
    derive a readable label from the provider/model value."""
    if not model_identifier or state._session_factory is None:
        return
    definition = find_model(model_identifier)
    split = provider_and_suffix(model_identifier)
    if definition is None and split is None:
        return
    if split is not None:
        provider, suffix = split
    else:
        assert definition is not None
        provider, suffix = definition.provider, definition.identifier.split("/", 1)[1]
    label = definition.name if definition is not None else suffix.replace("/", " / ").replace("-", " ").replace("_", " ").title()
    with sqlite_write_lock():
        database_session = state._session_factory()
        try:
            record = database_session.get(ModelHistoryRecord, model_identifier)
            selected_at = datetime.now(timezone.utc).isoformat()
            if record is None:
                database_session.add(ModelHistoryRecord(
                    model_id=model_identifier,
                    name=label,
                    provider=provider,
                    selected_at=selected_at,
                ))
            else:
                record.name = label
                record.provider = provider
                record.selected_at = selected_at
            database_session.commit()
        except Exception:
            database_session.rollback()
        finally:
            database_session.close()


def _recent_models(limit: int = 8) -> list[dict[str, str]]:
    """Recently selected models, newest first."""
    if state._session_factory is None:
        return []
    database_session = state._session_factory()
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


def _card_for(agent_name: str, working_directory: str = ""):
    """Build an agent's AgentCard from its config and the skills available to it.

    When a ``working_directory`` is given, skills are scoped to that path (home
    globals plus the path's own ``.agents``, deduped) rather than the server's
    launch directory — so a card advertises the skills a session in that folder
    can actually find. Without one, the server-CWD scoping is used (startup mount)."""
    assert state._global_configuration is not None
    configuration = load_agent_configuration(agent_name, state._global_configuration.agent_directories())
    skill_roots = (
        state._global_configuration.skill_directories_for(working_directory)
        if working_directory
        else state._global_configuration.skill_directories()
    )
    all_skills = load_skills(skill_roots)
    agent_skills = skills_for_agent(all_skills, configuration.skills)
    security_schemes, security = state._global_configuration.a2a.card_security()
    return configuration, build_agent_card(
        configuration, agent_skills, PUBLIC_BASE_URL,
        security_schemes=security_schemes, security=security,
    )


def _agent_directories_for_request(working_directory: str) -> list[Path]:
    assert state._global_configuration is not None
    return (
        state._global_configuration.agent_directories_for(working_directory)
        if working_directory
        else state._global_configuration.agent_directories()
    )


def _agent_configuration_for_request(agent_name: str, working_directory: str) -> tuple[Path, _configuration.AgentConfiguration]:
    directories = _agent_directories_for_request(working_directory)
    path = agent_configuration_path(agent_name, directories)
    return path, load_agent_configuration(agent_name, directories)


def _agent_configuration_payload(agent_name: str, working_directory: str) -> AgentConfigurationResponse:
    path, configuration = _agent_configuration_for_request(agent_name, working_directory)
    return AgentConfigurationResponse(
        id=configuration.identifier,
        name=configuration.name,
        title=configuration.display_name,
        model=configuration.model or "",
        provider=configuration.provider or "",
        reasoning_effort=configuration.reasoning_effort,
        permission_mode=configuration.permission_mode,
        stream_agent_progress=configuration.stream_agent_progress,
        tools_enabled=configuration.tools_enabled,
        bash=AgentBashConfigurationResponse(
            enabled=configuration.tools.bash.enabled,
            background_allowed=configuration.tools.bash.background_allowed,
            permissions=dict(configuration.tools.bash.permissions),
        ),
        spawn_agent=AgentSpawnConfigurationResponse(enabled=configuration.tools.spawn_agent.enabled),
        path=str(path.with_name("configuration.json")),
    )


def _load_agent_sidecar(agent_markdown_path: Path) -> dict[str, Any]:
    sidecar_path = agent_markdown_path.with_name("configuration.json")
    if not sidecar_path.exists():
        return {}
    data = json.loads(sidecar_path.read_text())
    return data if isinstance(data, dict) else {}


def _save_agent_sidecar(agent_markdown_path: Path, data: dict[str, Any]) -> None:
    sidecar_path = agent_markdown_path.with_name("configuration.json")
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _apply_agent_configuration_update(sidecar: dict[str, Any], request: AgentConfigurationUpdateRequest) -> dict[str, Any]:
    model = AgentSidecar.from_mapping(sidecar)
    if request.model is not None or request.provider is not None or request.reasoning_effort is not None:
        model.set_preset(
            model=request.model if request.model is not None else ...,
            provider=request.provider if request.provider is not None else ...,
            reasoning_effort=request.reasoning_effort if request.reasoning_effort is not None else ...,
        )
    if request.permission_mode is not None:
        model.permission_mode = request.permission_mode
    if request.stream_agent_progress is not None:
        model.stream_agent_progress = request.stream_agent_progress
    if request.tools_enabled is not None:
        model.set_tools_enabled(request.tools_enabled)
    if request.bash is not None:
        model.set_bash(
            enabled=request.bash.enabled if request.bash.enabled is not None else ...,
            background_allowed=request.bash.background_allowed if request.bash.background_allowed is not None else ...,
            permissions=(
                AgentSidecar.normalized_permissions(request.bash.permissions)
                if request.bash.permissions is not None
                else ...
            ),
        )
    if request.spawn_agent is not None:
        model.set_spawn_agent(
            enabled=request.spawn_agent.enabled if request.spawn_agent.enabled is not None else ...,
        )
    return model.to_mapping()


def _reload_agent_cards() -> None:
    """Recompile AgentCards from the agent markdown and skill files so discovery
    reflects edits without a restart. Agent behaviour itself is already live,
    since each turn loads its configuration and skills fresh."""
    assert state._global_configuration is not None and state._registry is not None
    for agent_name in list_agent_route_names(state._global_configuration.agent_directories()):
        handler = state._registry._handlers.get(agent_name)
        if handler is not None:
            _configuration, card = _card_for(agent_name)
            state._registry.register(agent_name, handler, card)
