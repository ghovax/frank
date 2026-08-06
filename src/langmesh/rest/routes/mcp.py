"""Mcp routes."""

from __future__ import annotations
from fastapi import APIRouter
import langmesh.base.configuration as _configuration
from langmesh.protocol.dtos import (
    MCPResourceReadRequest,
    MCPToolCallRequest,
)
from langmesh.hub import state
from langmesh.hub.brokers.mcp_servers import _ensure_mcp_servers_for

router = APIRouter()

@router.get("/mcp/tools")
async def mcp_tools(server: str = "", working_directory: str = ""):
    """The configured MCP servers with their tools; disabled ones are returned empty rather than hidden."""
    assert state.global_configuration is not None
    # Servers from the folder's own mcp.json are project-specific; home and Composio are global.
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
        # List the enabled servers and filter below: the manager raises for a name it does not hold.
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
