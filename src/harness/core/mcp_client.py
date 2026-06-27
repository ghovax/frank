import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

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
        raw_content = [_dump_model(content) for content in result.content]
        structured_content = result.structuredContent
        artifacts = _extract_artifacts(raw_content, structured_content)
        clean_structured_content = _strip_render_payloads(structured_content)
        return {
            "server": server,
            "tool": tool_name,
            "is_error": result.isError,
            "content": raw_content,
            "structured_content": clean_structured_content,
            "artifacts": artifacts,
            "model_context": _build_model_context(
                server=server,
                tool_name=tool_name,
                is_error=result.isError,
                content=raw_content,
                structured_content=clean_structured_content,
                artifacts=artifacts,
            ),
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
        raw_contents = [_dump_model(content) for content in result.contents]
        artifacts = _dedupe_artifacts([
            artifact
            for content in raw_contents
            for artifact in _artifact_from_resource_content(content)
        ])
        return {
            "server": server,
            "uri": uri,
            "contents": raw_contents,
            "artifacts": artifacts,
            "model_context": _build_resource_model_context(server, uri, raw_contents, artifacts),
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
            async with streamable_http_client(
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


_RENDER_PAYLOAD_KEYS = {"html", "iframe"}

_SUPPORTED_ARTIFACT_TYPES = {"html", "iframe", "image", "link"}


def _extract_artifacts(content: list[Any], structured_content: Any) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    artifacts.extend(_find_artifacts(structured_content))
    for entry in content:
        artifacts.extend(_artifact_from_mcp_content(entry))
        if isinstance(entry, dict) and entry.get("type") == "text":
            text = entry.get("text")
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    continue
                artifacts.extend(_find_artifacts(parsed))
    return _dedupe_artifacts(artifacts)


def _find_artifacts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [artifact for item in value for artifact in _find_artifacts(item)]
    if not isinstance(value, dict):
        return []

    normalized = _normalize_artifact(value)
    if normalized:
        return [normalized]
    nested = value.get("artifacts")
    return _find_artifacts(nested) if nested is not None else []


def _artifact_from_mcp_content(entry: Any) -> list[dict[str, Any]]:
    if not isinstance(entry, dict):
        return []
    content_type = entry.get("type")
    if content_type == "image":
        data = entry.get("data")
        mime_type = entry.get("mimeType") or entry.get("mime_type") or "image/png"
        if isinstance(data, str):
            return [{
                "type": "image",
                "title": "Image",
                "data": f"data:{mime_type};base64,{data}",
                "mime_type": mime_type,
            }]
    if content_type == "resource":
        resource = entry.get("resource")
        if isinstance(resource, dict):
            mime_type = resource.get("mimeType") or resource.get("mime_type") or ""
            text = resource.get("text")
            blob = resource.get("blob")
            uri = resource.get("uri")
            if isinstance(text, str) and mime_type in ("text/html", "application/xhtml+xml"):
                return [{
                    "type": "html",
                    "title": str(uri or "HTML resource"),
                    "html": text,
                    "mime_type": mime_type,
                }]
            if isinstance(blob, str) and isinstance(mime_type, str) and mime_type.startswith("image/"):
                return [{
                    "type": "image",
                    "title": str(uri or "Image resource"),
                    "data": f"data:{mime_type};base64,{blob}",
                    "mime_type": mime_type,
                }]
    return []


def _artifact_from_resource_content(entry: Any) -> list[dict[str, Any]]:
    if not isinstance(entry, dict):
        return []
    mime_type = entry.get("mimeType") or entry.get("mime_type") or ""
    text = entry.get("text")
    blob = entry.get("blob")
    uri = entry.get("uri")
    if isinstance(text, str) and mime_type in ("text/html", "application/xhtml+xml"):
        return [{
            "type": "html",
            "title": str(uri or "HTML resource"),
            "html": text,
            "mime_type": mime_type,
        }]
    if isinstance(blob, str) and isinstance(mime_type, str) and mime_type.startswith("image/"):
        return [{
            "type": "image",
            "title": str(uri or "Image resource"),
            "data": f"data:{mime_type};base64,{blob}",
            "mime_type": mime_type,
        }]
    return []


def _normalize_artifact(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    artifact_type = value.get("type")
    if not isinstance(artifact_type, str):
        if isinstance(value.get("src"), str) or isinstance(value.get("srcdoc"), str):
            artifact_type = "iframe"
        elif isinstance(value.get("html"), str):
            artifact_type = "html"
        elif isinstance(value.get("data"), str) or isinstance(value.get("url"), str):
            artifact_type = "image"
        else:
            return None
    artifact_type = artifact_type.lower().strip()
    if artifact_type not in _SUPPORTED_ARTIFACT_TYPES:
        return None

    normalized = dict(value)
    normalized["type"] = artifact_type
    if "mimeType" in normalized and "mime_type" not in normalized:
        normalized["mime_type"] = normalized.pop("mimeType")
    return normalized


def _dedupe_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for artifact in artifacts:
        normalized = _normalize_artifact(artifact)
        if not normalized:
            continue
        key = json.dumps(normalized, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped


def _strip_render_payloads(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_render_payloads(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _strip_render_payloads(item)
            for key, item in value.items()
            if key not in _RENDER_PAYLOAD_KEYS
        }
    return value


def _build_model_context(
    *,
    server: str,
    tool_name: str,
    is_error: bool | None,
    content: list[Any],
    structured_content: Any,
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "server": server,
        "tool": tool_name,
        "is_error": bool(is_error),
    }
    if isinstance(structured_content, dict) and structured_content.get("context") is not None:
        context["context"] = structured_content["context"]
    elif structured_content not in (None, {}, []):
        context["structured_content"] = structured_content

    text_entries = _content_for_context(content)
    if text_entries:
        context["content"] = text_entries
    if artifacts:
        context["artifacts"] = [
            {
                key: artifact.get(key)
                for key in ("type", "title", "mime_type", "width", "height", "summary")
                if artifact.get(key) is not None
            }
            for artifact in artifacts
        ]
    return context


def _content_for_context(content: list[Any]) -> list[Any]:
    context_entries: list[Any] = []
    for entry in content:
        if not isinstance(entry, dict):
            context_entries.append(entry)
            continue
        content_type = entry.get("type")
        if content_type == "text":
            text = entry.get("text")
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    context_entries.append({"type": "text", "text": text})
                else:
                    context_entries.append(_strip_render_payloads(parsed))
        elif content_type == "image":
            context_entries.append({
                "type": "image",
                "mime_type": entry.get("mimeType") or entry.get("mime_type"),
            })
        elif content_type == "resource":
            resource = entry.get("resource")
            if isinstance(resource, dict):
                context_entries.append({
                    "type": "resource",
                    "uri": resource.get("uri"),
                    "mime_type": resource.get("mimeType") or resource.get("mime_type"),
                })
    return context_entries


def _build_resource_model_context(
    server: str,
    uri: str,
    contents: list[Any],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "server": server,
        "uri": uri,
        "contents": [_resource_content_for_context(content) for content in contents],
    }
    if artifacts:
        context["artifacts"] = [
            {
                key: artifact.get(key)
                for key in ("type", "title", "mime_type", "width", "height", "summary")
                if artifact.get(key) is not None
            }
            for artifact in artifacts
        ]
    return context


def _resource_content_for_context(content: Any) -> Any:
    if not isinstance(content, dict):
        return content
    mime_type = content.get("mimeType") or content.get("mime_type")
    if isinstance(mime_type, str) and (mime_type.startswith("image/") or mime_type in ("text/html", "application/xhtml+xml")):
        return {
            "uri": content.get("uri"),
            "mime_type": mime_type,
        }
    return content
