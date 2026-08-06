"""Mcp routes."""

from __future__ import annotations
from fastapi import APIRouter
import frank.base.configuration as _configuration
from frank.protocol.dtos import (
    MCPResourceReadRequest,
    MCPToolCallRequest,
)
from frank.hub import state
from frank.hub.brokers.mcp_servers import _ensure_mcp_servers_for

router = APIRouter()

@router.get("/mcp/tools")
async def mcp_tools(server: str = "", working_directory: str = ""):
    """List configured MCP servers with their enabled flag. Enabled servers carry
    their advertised tools; disabled ones are still returned (with no tools) so the
    UI can show them greyed out rather than hiding them.

    Scoped to the selected folder when ``working_directory`` is given: only the
    servers that folder declares (its own ``mcp.json`` plus the home globals) and
    the global Composio integration are listed — the launch directory's servers do
    not leak in. The folder's servers are ensured running first so their tools
    actually appear (the subprocess pool is shared and grows as a union)."""
    assert state.global_configuration is not None
    # Servers declared by the working directory's own mcp.json are project-specific; everything else (home globals and the Composio integration) is global.
    project_server_names: set[str] = set()
    if working_directory:
        await _ensure_mcp_servers_for(working_directory)
        allowed = set(state.global_configuration.mcp_configuration_for(working_directory).servers)
        allowed.update(state.composio_servers)
        configured = {
            name: configuration
            for name, configuration in state.global_configuration.mcp.servers.items()
            if name in allowed
        }
        home_root = state.global_configuration.home_agents_root().resolve()
        project_root = state.global_configuration.project_agents_root_for(working_directory).resolve()
        if project_root != home_root:
            project_server_names = set(
                _configuration.MCPConfiguration.from_dotagents_roots([project_root]).servers
            )
    else:
        configured = state.global_configuration.mcp.servers
    tools_by_server: dict[str, list] = {}
    if state.mcp_manager is not None:
        # List every enabled server, then filter below — querying the manager for a disabled server name would raise, since it only holds enabled ones.
        listing = await state.mcp_manager.list_tools("")
        tools_by_server = {entry["name"]: entry["tools"] for entry in listing["servers"]}
    servers = [
        {
            "name": name,
            "enabled": configuration.enabled,
            "tools": tools_by_server.get(name, []),
            "scope": "project" if name in project_server_names else "global",
        }
        for name, configuration in sorted(configured.items())
        if not server or name == server
    ]
    return {"servers": servers}


@router.get("/mcp/resources")
async def mcp_resources(server: str = ""):
    """List resources exposed by configured MCP servers."""
    if state.mcp_manager is None:
        return {"servers": []}
    return await state.mcp_manager.list_resources(server)


@router.post("/mcp/tools/call")
async def mcp_call_tool(request: MCPToolCallRequest):
    """Call a configured MCP server tool. Intended for smoke tests and UI discovery."""
    if state.mcp_manager is None:
        return {"error": "MCP is not configured."}
    return await state.mcp_manager.call_tool(request.server, request.tool_name, request.arguments)


@router.post("/mcp/resources/read")
async def mcp_read_resource(request: MCPResourceReadRequest):
    """Read a configured MCP resource. Intended for smoke tests and UI discovery."""
    if state.mcp_manager is None:
        return {"error": "MCP is not configured."}
    return await state.mcp_manager.read_resource(request.server, request.uri)
