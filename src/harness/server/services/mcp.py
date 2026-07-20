"""Mcp service: helpers split out of the server runtime."""

from harness.core.configuration import GlobalConfiguration
from harness.core.mcp_client import MCPClientManager
from harness.tools.tools import set_mcp_client_manager
from harness.server import state


async def _reload_mcp() -> None:
    """Re-read mcp.json and apply the server set live: reconcile the client manager
    (start new servers, stop removed/disabled ones, keep unchanged ones connected)
    and drop cached runtimes so the next turn rebuilds its tools with the new set.
    No server restart required."""
    assert state._global_configuration is not None
    # Serialize with the settings endpoints and the configuration watcher: they all
    # rebuild the shared `_mcp_manager`, so overlapping runs would clobber it.
    async with state._configuration_lock:
        state._global_configuration.mcp = GlobalConfiguration.load().mcp
        # Re-fold the startup-provisioned Composio server back in so a live mcp.json
        # edit doesn't drop Composio's tools (and the agent keeps its MCP tools).
        state._global_configuration.mcp.servers.update(state._composio_servers)
        enabled = state._global_configuration.mcp.enabled_servers()
        if state._mcp_manager is None:
            if enabled:
                state._mcp_manager = MCPClientManager(enabled)
                await state._mcp_manager.start()
                set_mcp_client_manager(state._mcp_manager)
        else:
            await state._mcp_manager.reconcile(enabled)
        for executor in state._executors.values():
            executor.reset_runtimes()


async def _ensure_mcp_servers_for(working_directory: str) -> None:
    """Additively grow the shared MCP server pool with the working directory's own
    ``mcp.json`` servers, so a folder's servers are running and listable once that
    folder is selected. The pool is a union — servers are only added or updated,
    never removed — so no other session loses its servers."""
    assert state._global_configuration is not None
    if not working_directory:
        return
    # Serialize with every other `_mcp_manager` mutator (settings save, config/mcp.json
    # watchers) so concurrent reconciles never clobber the shared manager.
    async with state._configuration_lock:
        folder_servers = state._global_configuration.mcp_configuration_for(working_directory).servers
        new_servers = {
            name: configuration
            for name, configuration in folder_servers.items()
            if state._global_configuration.mcp.servers.get(name) != configuration
        }
        if not new_servers:
            return
        state._global_configuration.mcp.servers.update(new_servers)
        enabled = state._global_configuration.mcp.enabled_servers()
        if state._mcp_manager is None:
            if enabled:
                state._mcp_manager = MCPClientManager(enabled)
                await state._mcp_manager.start()
                set_mcp_client_manager(state._mcp_manager)
        else:
            await state._mcp_manager.reconcile(enabled)
        for executor in state._executors.values():
            executor.reset_runtimes()
