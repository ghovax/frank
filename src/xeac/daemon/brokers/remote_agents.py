"""Remote-agent domain: loading the remote-agents configuration, reloading it, and polling
remote-agent health."""

from __future__ import annotations

from xeac.base.configuration import GlobalConfiguration
from xeac.protocol.client import RemoteAgentAuth
from xeac.protocol.client import RemoteAgentConfiguration
from xeac.protocol.client import RemoteAgentManager
import asyncio
from xeac.daemon import state
from xeac.daemon.services.broadcast import _publish_broadcast


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
    """Re-read remote-agents.json and apply the external-agent set live: reconcile the
    outbound client manager and drop cached runtimes so the next turn's roster reflects
    the change. No server restart required."""
    assert state.global_configuration is not None and state.registry is not None
    async with state.configuration_lock:
        state.global_configuration.remote_agents = GlobalConfiguration.load().remote_agents
        configurations = _remote_agent_dataclasses()
        if state.remote_agent_manager is None:
            state.remote_agent_manager = RemoteAgentManager(configurations)
            await state.remote_agent_manager.start()
            state.registry.set_remote_manager(state.remote_agent_manager)
        else:
            await state.remote_agent_manager.reconcile(configurations)
        for executor in state._executors.values():
            executor.reset_runtimes()
        state.broadcaster.publish({"type": "remote_agents_changed"})


async def _poll_remote_agent_health(interval_seconds: float = 300.0) -> None:
    """Periodically re-resolve remote agent cards so their health stays current in the UI
    even while idle, broadcasting on each pass so open panels refresh."""
    while True:
        await asyncio.sleep(interval_seconds)
        if state.remote_agent_manager is not None and state.remote_agent_manager.has_agents():
            await state.remote_agent_manager.refresh_all()
            _publish_broadcast({"type": "remote_agents_changed"})
