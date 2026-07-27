"""Agent domain: card building, per-request configuration resolution, sidecar
read/write, configuration updates, and model-selection history."""

from __future__ import annotations
from daisy.protocol.dtos import (
    AgentBashConfigurationResponse,
    AgentConfigurationResponse,
    AgentConfigurationUpdateRequest,
)

from datetime import datetime, timezone
from daisy.protocol.card import build_agent_card
from daisy.base.configuration import (
    agent_configuration_path,
    AgentSidecar,
    list_agent_route_names,
    load_agent_configuration,
)
from daisy.base.models import find_model, provider_and_suffix
from daisy.base.skills import load_skills, skills_for_agent
from daisy.base.sqlite_lock import sqlite_write_lock
from pathlib import Path
from typing import Any
import daisy.base.configuration as _configuration
import json
from daisy.workspace import state
from daisy.workspace.database import ModelHistoryRecord


def _catalogue_base_url() -> str:
    """The address a card from this catalogue names.

    These are agent *profiles*, not running sessions — nothing is listening on their
    behalf until one is created, and a created session advertises its own socket instead.
    So the honest address is the daemon that would create it, which binds an ephemeral
    port chosen at boot; a fixed one baked in here would name whatever else happened to
    take that number."""
    return f"http://127.0.0.1:{state.daemon_port}"


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
    if not model_identifier or state.session_factory is None:
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
        database_session = state.session_factory()
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
    if state.session_factory is None:
        return []
    database_session = state.session_factory()
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
    globals plus the path's own ``.agents``, deduped) rather than the daemon's
    launch directory — so a card advertises the skills a session in that folder
    can actually find. Without one, the daemon-CWD scoping is used (startup mount)."""
    assert state.global_configuration is not None
    configuration = load_agent_configuration(agent_name, state.global_configuration.agent_directories())
    skill_roots = (
        state.global_configuration.skill_directories_for(working_directory)
        if working_directory
        else state.global_configuration.skill_directories()
    )
    all_skills = load_skills(skill_roots)
    agent_skills = skills_for_agent(all_skills, configuration.skills)
    return configuration, build_agent_card(
        configuration, agent_skills, _catalogue_base_url(),
    )


def _agent_directories_for_request(working_directory: str) -> list[Path]:
    assert state.global_configuration is not None
    return (
        state.global_configuration.agent_directories_for(working_directory)
        if working_directory
        else state.global_configuration.agent_directories()
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
        tools_enabled=configuration.tools_enabled,
        tools_disabled=list(configuration.tools.disabled),
        bash=AgentBashConfigurationResponse(
            enabled=configuration.tools.bash.enabled,
            background_allowed=configuration.tools.bash.background_allowed,
            permissions=dict(configuration.tools.bash.permissions),
        ),
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
    if request.tools_enabled is not None:
        model.set_tools_enabled(request.tools_enabled)
    if request.tools_disabled is not None:
        model.set_tools_disabled(request.tools_disabled)
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
    return model.to_mapping()


def _reload_agent_cards() -> None:
    """Recompile the catalogue of AgentCards from the agent and skill files on disk.

    This describes the agent *profiles* a session could be created with, which is a
    different thing from the sessions themselves — a running session serves its own card on
    its own socket. Kept current so discovery reflects an edit without a restart; agent
    behaviour is already live, since every turn loads its configuration and skills fresh."""
    assert state.global_configuration is not None
    catalogue = {}
    for agent_name in list_agent_route_names(state.global_configuration.agent_directories()):
        try:
            _configuration, card = _card_for(agent_name)
        except Exception:  # noqa: BLE001 — one unreadable profile must not empty the catalogue
            continue
        catalogue[agent_name] = card
    state.agent_cards = catalogue
