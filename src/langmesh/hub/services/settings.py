"""The settings domain: applying live credentials, and persisting and reloading the configuration file."""

from __future__ import annotations

from langmesh.hub.brokers.composio import composio_mcp_servers
from langmesh.base.configuration import Configuration, save_api_keys
from langmesh.base.paths import configuration_file_path
from langmesh.base.mcp_client import MCPClientManager
from langmesh.base.serialization import compact
from typing import Optional
import asyncio
import hashlib
from langmesh.hub import state
from langmesh.hub.services.sessions import _reset_work_habits_acknowledgements


async def _apply_live_credentials() -> None:
    """Re-provision what the daemon itself owns after a configuration change."""
    assert state.global_configuration is not None
    configuration = state.global_configuration
    state.composio_servers = composio_mcp_servers(configuration.composio)
    if state.composio_servers:
        configuration.mcp.servers.update(state.composio_servers)
    else:
        configuration.mcp.servers.pop(configuration.composio.server_name, None)
    mcp_servers = configuration.mcp.enabled_servers()
    # Only when the servers themselves changed. Every write used to close every connection and open
    # it again — new subprocesses, new handshakes — so setting an unrelated number cost as much as
    # editing the servers, and the cost was paid while the caller waited.
    fingerprint = _mcp_server_fingerprint(mcp_servers)
    if fingerprint == state.mcp_server_fingerprint and state.mcp_manager is not None:
        return
    if state.mcp_manager is not None:
        await state.mcp_manager.aclose()
    state.mcp_manager = MCPClientManager(mcp_servers) if mcp_servers else None
    if state.mcp_manager is not None:
        await state.mcp_manager.start()
    state.mcp_server_fingerprint = fingerprint


def _mcp_server_fingerprint(servers: dict) -> str:
    """What the MCP connections are built from, as one comparable string."""
    return compact(
        {
            name: server.model_dump(mode="json") if hasattr(server, "model_dump") else server
            for name, server in sorted(servers.items())
        },
        sort_keys=True,
    )


def _configuration_digest() -> Optional[str]:
    """A content hash of the configuration file, or ``None`` if it is absent."""
    try:
        return hashlib.sha256(configuration_file_path().read_bytes()).hexdigest()
    except OSError:
        return None


async def _persist_configuration(**changes) -> None:
    """Write configuration changes to disk and remember the digest, so the watcher does not read our own save as an edit."""
    await asyncio.to_thread(save_api_keys, **changes)
    state.last_written_configuration_digest = await asyncio.to_thread(_configuration_digest)


async def _reload_configuration_from_disk() -> None:
    """Re-read the configuration file after a manual edit and apply it live."""
    assert state.global_configuration is not None
    fresh = await asyncio.to_thread(Configuration.load)
    configuration = state.global_configuration
    user_context_setting_changed = configuration.user_context.enabled != fresh.user_context.enabled
    # Every section, from the model rather than from a hand-kept list that fell behind the schema.
    for name in type(fresh).model_fields:
        setattr(configuration, name, getattr(fresh, name))
    await _apply_live_credentials()
    if user_context_setting_changed:
        await asyncio.to_thread(_reset_work_habits_acknowledgements)
    state.broadcaster.publish({"type": "settings_changed"})
