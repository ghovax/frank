"""call_remote_agent is a distinct tool, offered only when a remote agent is available."""

from harness.core.agent import _build_tools
from harness.core.configuration import (
    AgentConfiguration,
    GlobalConfiguration,
    RemoteAgentServerConfiguration,
    RemoteAgentsConfiguration,
)


def _agent(name: str) -> AgentConfiguration:
    configuration = AgentConfiguration(name=name)
    configuration.tools.spawn_agent.enabled = True
    return configuration


def _global_with_remote(**agent_kwargs) -> GlobalConfiguration:
    configuration = GlobalConfiguration()
    configuration.remote_agents = RemoteAgentsConfiguration(
        agents={"acme": RemoteAgentServerConfiguration(card_url="https://a.example/.well-known/agent-card.json", **agent_kwargs)}
    )
    return configuration


def test_tool_offered_when_a_remote_agent_is_available():
    names = {tool.name for tool in _build_tools(_agent("coder"), _global_with_remote())}
    assert "call_remote_agent" in names
    assert "spawn_agent" in names


def test_tool_absent_without_any_remote_agent():
    names = {tool.name for tool in _build_tools(_agent("coder"), GlobalConfiguration())}
    assert "call_remote_agent" not in names
    assert "spawn_agent" in names


def test_tool_respects_per_profile_allow_list():
    global_configuration = _global_with_remote(allowed_profiles=["researcher"])
    allowed = {tool.name for tool in _build_tools(_agent("researcher"), global_configuration)}
    denied = {tool.name for tool in _build_tools(_agent("coder"), global_configuration)}
    assert "call_remote_agent" in allowed
    assert "call_remote_agent" not in denied
