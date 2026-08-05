"""Compiling an agent definition into the A2A AgentCard a session advertises.

Every session serves its card at the well-known path, so a peer that holds the session's
address can discover what it is and what it can do without any out-of-band registry.
"""

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
    """Compile an agent's markdown definition into the AgentCard its session advertises.

    Each running session serves this at its own well-known path, so it is independently
    discoverable by whoever holds its address.

    The agent's available skills (discovered from the skills directory) are
    advertised on the card; if there are none, a single default skill describing
    the agent's role is synthesised so the card always carries at least one skill.
    """
    display_name = configuration.display_name
    capability = (
        "Investigates and reports read-only — cannot modify the system."
        # A card with no ceiling is not read-only; it simply has not said.
        if configuration.permission_policy is not None and configuration.permission_policy.is_read_only
        else "Can read and modify the system."
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
