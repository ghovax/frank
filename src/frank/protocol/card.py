"""Compiling an agent definition into the A2A AgentCard a session advertises."""

from __future__ import annotations


from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentProvider,
    AgentSkill,
)

from frank.base.configuration import AgentConfiguration
from frank.base.skills import Skill
from frank.protocol.metadata import METADATA_KEY


def build_agent_card(
    configuration: AgentConfiguration,
    available_skills: list[Skill],
    base_url: str,
) -> AgentCard:
    """Compile an agent's markdown definition into the AgentCard its session advertises."""
    display_name = configuration.display_name
    # What the profile's own confinement permits.
    sandbox = getattr(configuration, "sandbox", None)
    writes = sandbox is None or bool(sandbox.filesystem.writable)
    capability = (
        "Can read and modify the system." if writes
        else "Investigates and reports — its confinement permits no writes."
    )
    skills = [
        AgentSkill(
            id=skill.identifier,
            name=skill.identifier,
            description=skill.description or skill.display_title,
            tags=["harness", "skill"],
        )
        for skill in available_skills
    ]
    if not skills:
        skills.append(
            AgentSkill(
                id=configuration.identifier,
                name=configuration.identifier,
                description=(configuration.description or display_name) + f" {capability}",
                tags=["harness", configuration.permission_mode or "unbounded", configuration.model or "unconfigured-model"],
                examples=[f"Ask {display_name} to help with a task in its domain."],
            )
        )
    url = base_url
    return AgentCard(
        name=configuration.identifier,
        description=configuration.description or f"The '{display_name}' agent.",
        url=url,
        version="1.0.0",
        protocol_version="0.3.0",
        preferred_transport="JSONRPC",
        additional_interfaces=[AgentInterface(transport="JSONRPC", url=url)],
        provider=AgentProvider(organization="Frank", url="https://github.com/ghovax/frank"),
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "text/markdown", "application/json"],
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=True,
            state_transition_history=True,
            extensions=[AgentExtension(
                uri=METADATA_KEY,
                description=(
                    "Frank turn state and envelopes. Under this key: a message's per-turn inputs "
                    "(working directory, permission mode, peer sender), a task's durable "
                    "control-state (turn kind, peer sender, pending interaction, referenced "
                    "turns), and the payload of every DataPart the harness emits or reads."
                ),
                required=False,
            )],
        ),
        skills=skills,
    )
