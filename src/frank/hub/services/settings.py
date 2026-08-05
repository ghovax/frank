"""Settings domain: applying live credentials to the running clients, and persisting and
reloading the configuration file."""

from __future__ import annotations

from frank.hub.brokers.composio import composio_mcp_servers
from frank.base.configuration import Configuration, save_api_keys
from frank.base.paths import configuration_file_path
from frank.base.mcp_client import MCPClientManager
from typing import Optional
import asyncio
import hashlib
from frank.hub import state
from frank.hub.services.sessions import _reset_work_habits_acknowledgements


async def _apply_live_credentials() -> None:
    """Re-provision what the daemon itself owns after a configuration change.

    That is now only the shared MCP set: the Composio server is rebuilt, folded into (or
    removed from) the configured servers, and the client manager restarted. The credential-
    dependent tool clients live in workers, and a worker reads the configuration when it
    starts, so nothing needs pushing into a running one — the sessions already running keep
    the credentials they were started with, which is the same guarantee their permission mode
    carries."""
    assert state.global_configuration is not None
    configuration = state.global_configuration
    state.composio_servers = composio_mcp_servers(configuration.composio)
    if state.composio_servers:
        configuration.mcp.servers.update(state.composio_servers)
    else:
        configuration.mcp.servers.pop(configuration.composio.server_name, None)
    if state.mcp_manager is not None:
        await state.mcp_manager.aclose()
    mcp_servers = configuration.mcp.enabled_servers()
    state.mcp_manager = MCPClientManager(mcp_servers) if mcp_servers else None
    if state.mcp_manager is not None:
        await state.mcp_manager.start()


def _configuration_digest() -> Optional[str]:
    """A content hash of the configuration file, or ``None`` if it is absent."""
    try:
        return hashlib.sha256(configuration_file_path().read_bytes()).hexdigest()
    except OSError:
        return None


async def _persist_configuration(**changes) -> None:
    """Write configuration changes to disk and remember the resulting content digest so
    the on-disk watcher does not treat our own save as an external edit."""
    await asyncio.to_thread(save_api_keys, **changes)
    state.last_written_configuration_digest = await asyncio.to_thread(_configuration_digest)


async def _reload_configuration_from_disk() -> None:
    """Re-read the configuration file after a manual on-disk edit and apply it live:
    refresh the in-memory credentials/settings, rebuild the credential-dependent clients,
    and broadcast so every connected client refetches. The MCP server *set* (mcp.json plus
    any folder-added servers) is left to its own watcher — only the credential-derived
    Composio server is re-provisioned here."""
    assert state.global_configuration is not None
    fresh = await asyncio.to_thread(Configuration.load)
    configuration = state.global_configuration
    user_context_setting_changed = configuration.user_context.enabled != fresh.user_context.enabled
    # Every section, from the model rather than from a list kept by hand. The list was the
    # bug: it named twelve sections and the schema had grown to nineteen, so `toolbox`,
    # `permission_classifier`, `telemetry`, `mcp` and `remote_agents` were read off disk into
    # `fresh` and then dropped on the floor — a setting written to the file that took effect
    # only at the next daemon start, with nothing saying so. Mutated in place rather than
    # reassigned, because callers hold this object.
    for name in type(fresh).model_fields:
        setattr(configuration, name, getattr(fresh, name))
    await _apply_live_credentials()
    if user_context_setting_changed:
        await asyncio.to_thread(_reset_work_habits_acknowledgements)
    state.broadcaster.publish({"type": "settings_changed"})
