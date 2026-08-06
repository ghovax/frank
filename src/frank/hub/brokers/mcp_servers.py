"""MCP domain: per-request resolution of an agent's servers, and the live reload of mcp.json."""

from __future__ import annotations

from frank.base.configuration import Configuration
from frank.base.mcp_client import MCPClientManager
from frank.hub import state


async def _reload_mcp() -> None:
    """Re-read mcp.json and apply it live, dropping cached runtimes so the next turn sees the new set."""
    assert state.global_configuration is not None
    # Serialized with the settings endpoints and the watcher, which all rebuild the shared manager.
    async with state.configuration_lock:
        state.global_configuration.mcp = Configuration.load().mcp
        # Re-fold the provisioned Composio server in, so a live edit does not drop its tools.
        state.global_configuration.mcp.servers.update(state.composio_servers)
        enabled = state.global_configuration.mcp.enabled_servers()
        if state.mcp_manager is None:
            if enabled:
                state.mcp_manager = MCPClientManager(enabled)
                await state.mcp_manager.start()
        else:
            await state.mcp_manager.reconcile(enabled)
        await state.reset_runtimes()


async def _ensure_mcp_servers_for(working_directory: str) -> None:
    """Grow the shared pool with a folder's own servers. A union: servers are added or updated, never removed."""
    assert state.global_configuration is not None
    if not working_directory:
        return
    # Serialized with every other mutator, so concurrent reconciles never clobber the shared manager.
    async with state.configuration_lock:
        folder_servers = state.global_configuration.mcp_configuration_for(working_directory).servers
        new_servers = {
            name: configuration
            for name, configuration in folder_servers.items()
            if state.global_configuration.mcp.servers.get(name) != configuration
        }
        if not new_servers:
            return
        state.global_configuration.mcp.servers.update(new_servers)
        enabled = state.global_configuration.mcp.enabled_servers()
        if state.mcp_manager is None:
            if enabled:
                state.mcp_manager = MCPClientManager(enabled)
                await state.mcp_manager.start()
        else:
            await state.mcp_manager.reconcile(enabled)
        await state.reset_runtimes()
