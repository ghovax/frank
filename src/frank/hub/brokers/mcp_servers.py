"""MCP domain: per-request resolution of an agent's MCP servers, and the live reload that re-merges mcp.json with the Composio-provisioned servers."""

from __future__ import annotations

from frank.base.configuration import Configuration
from frank.base.mcp_client import MCPClientManager
from frank.hub import state


async def _reload_mcp() -> None:
    """Re-read mcp.json and apply the daemon set live: reconcile the client manager (start new servers, stop removed/disabled ones, keep unchanged ones connected) and drop cached runtimes so the next turn rebuilds its tools with the new set."""
    assert state.global_configuration is not None
    # Serialize with the settings endpoints and the configuration watcher: they all rebuild the shared `_mcp_manager`, so overlapping runs would clobber it.
    async with state.configuration_lock:
        state.global_configuration.mcp = Configuration.load().mcp
        # Re-fold the startup-provisioned Composio server back in so a live mcp.json edit doesn't drop Composio's tools (and the agent keeps its MCP tools).
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
    """Additively grow the shared MCP server pool with the working directory's own ``mcp.json`` servers, so a folder's servers are running and listable once that folder is selected."""
    assert state.global_configuration is not None
    if not working_directory:
        return
    # Serialize with every other `_mcp_manager` mutator (settings save, config/mcp.json watchers) so concurrent reconciles never clobber the shared manager.
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
