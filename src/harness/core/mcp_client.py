import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from harness.core.configuration import MCPServerConfiguration


class MCPClientManager:
    """Small MCP client facade for configured servers.

    Connections are opened per operation. That keeps stdio process lifetimes
    simple and makes server edits visible without restart coordination.
    """

    def __init__(self, servers: dict[str, MCPServerConfiguration]):
        self._servers = servers

    @property
    def has_servers(self) -> bool:
        return bool(self._servers)

    def server_names(self) -> list[str]:
        return sorted(self._servers)

    async def list_tools(self, server: str = "") -> dict[str, Any]:
        result: dict[str, Any] = {"servers": []}
        for name in self._selected_servers(server):
            async with self._session(name) as session:
                tools_result = await session.list_tools()
                result["servers"].append({
                    "name": name,
                    "tools": [
                        {
                            "name": tool.name,
                            "title": tool.title,
                            "description": tool.description,
                            "input_schema": tool.inputSchema,
                        }
                        for tool in tools_result.tools
                    ],
                })
        return result

    async def call_tool(self, server: str, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if not server:
            raise ValueError("server is required when calling an MCP tool")
        async with self._session(server) as session:
            result = await session.call_tool(tool_name, arguments or {})
        return {
            "server": server,
            "tool": tool_name,
            "is_error": result.isError,
            "content": [_dump_model(content) for content in result.content],
            "structured_content": result.structuredContent,
        }

    async def list_resources(self, server: str = "") -> dict[str, Any]:
        result: dict[str, Any] = {"servers": []}
        for name in self._selected_servers(server):
            async with self._session(name) as session:
                resources_result = await session.list_resources()
                result["servers"].append({
                    "name": name,
                    "resources": [
                        {
                            "uri": str(resource.uri),
                            "name": resource.name,
                            "title": resource.title,
                            "description": resource.description,
                            "mime_type": resource.mimeType,
                        }
                        for resource in resources_result.resources
                    ],
                })
        return result

    async def read_resource(self, server: str, uri: str) -> dict[str, Any]:
        if not server:
            raise ValueError("server is required when reading an MCP resource")
        async with self._session(server) as session:
            result = await session.read_resource(uri)
        return {
            "server": server,
            "uri": uri,
            "contents": [_dump_model(content) for content in result.contents],
        }

    def _selected_servers(self, server: str) -> list[str]:
        if server:
            if server not in self._servers:
                raise ValueError(f"Unknown MCP server: {server}")
            return [server]
        return self.server_names()

    @asynccontextmanager
    async def _session(self, server_name: str) -> AsyncIterator[ClientSession]:
        configuration = self._servers.get(server_name)
        if configuration is None:
            raise ValueError(f"Unknown MCP server: {server_name}")
        if configuration.transport == "stdio":
            if not configuration.command:
                raise ValueError(f"MCP server '{server_name}' is missing command")
            parameters = StdioServerParameters(
                command=configuration.command,
                args=configuration.args,
                env=configuration.env or None,
                cwd=str(Path(configuration.cwd).expanduser()) if configuration.cwd else None,
            )
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    yield session
            return
        if configuration.transport == "streamable_http":
            if not configuration.url:
                raise ValueError(f"MCP server '{server_name}' is missing url")
            async with streamablehttp_client(
                configuration.url,
                headers=configuration.headers or None,
                timeout=configuration.timeout_seconds,
            ) as (read_stream, write_stream, _session_id):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    yield session
            return
        raise ValueError(f"Unsupported MCP transport for '{server_name}': {configuration.transport}")


def _dump_model(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)
