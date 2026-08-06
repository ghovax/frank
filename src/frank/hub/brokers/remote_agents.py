"""Remote-agent domain: loading the remote-agents configuration, reloading it, and polling remote-agent health."""

from __future__ import annotations

from frank.base.configuration import Configuration
from frank.protocol.client import RemoteAgentAuth, RemoteAgentConfiguration, RemoteAgentManager
from frank.hub import state


def _remote_agent_dataclasses() -> dict[str, RemoteAgentConfiguration]:
    """Convert the loaded ``remote-agents.json`` config into the manager's dataclasses."""
    assert state.global_configuration is not None
    result: dict[str, RemoteAgentConfiguration] = {}
    for name, configuration in state.global_configuration.remote_agents.enabled_agents().items():
        auth = configuration.auth
        result[name] = RemoteAgentConfiguration(
            name=name,
            card_url=configuration.card_url,
            auth=RemoteAgentAuth(
                kind=auth.type, token=auth.token, header=auth.header, scheme_prefix=auth.scheme_prefix,
                token_url=auth.token_url, client_id=auth.client_id, client_secret=auth.client_secret,
                scopes=list(auth.scopes),
            ),
            card_ttl_seconds=configuration.card_ttl_seconds,
            allowed_hosts=list(configuration.allowed_hosts),
            allow_private=configuration.allow_private,
            allowed_profiles=list(configuration.allowed_profiles),
        )
    return result


async def _reload_remote_agents() -> None:
    """Re-read remote-agents.json and apply the external-agent set live: reconcile the outbound client manager and drop cached runtimes so the next turn's roster reflects the change."""
    assert state.global_configuration is not None
    async with state.configuration_lock:
        state.global_configuration.remote_agents = Configuration.load().remote_agents
        configurations = _remote_agent_dataclasses()
        if state.remote_agent_manager is None:
            state.remote_agent_manager = RemoteAgentManager(configurations)
            await state.remote_agent_manager.start()
        else:
            await state.remote_agent_manager.reconcile(configurations)
        await state.reset_runtimes()
        state.broadcaster.publish({"type": "remote_agents_changed"})

