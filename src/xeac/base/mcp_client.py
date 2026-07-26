from __future__ import annotations

import json
import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.session import RequestResponder
from pydantic import AnyUrl


# How long to wait for one MCP server to complete its connect + initialize handshake
# on startup before giving up on it and booting without it. Bounds a hung spawn/endpoint
# so a single unresponsive server can never freeze the whole harness boot.

from xeac.base.configuration import MCPServerConfiguration
from xeac.base.tuning import Limit, active_tuning

logger = logging.getLogger(__name__)

MCPEventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


def _as_connection_failure(error: BaseException, server_name: str) -> BaseException:
    """Turn a cancellation raised *by* a failed connect into a real error.

    The MCP client libraries build on anyio task groups, and a server that goes away
    mid-handshake unwinds those groups by cancelling — which surfaces here as a bare
    `CancelledError`. That is not our caller asking to stop; it is the connection failing, and
    letting it propagate as cancellation makes a broken MCP server look like an abort and
    escape every `except Exception` a caller reasonably wrote. A cancellation that our own
    task actually requested is passed through untouched, which `Task.cancelling` is exactly
    the signal for."""
    if not isinstance(error, asyncio.CancelledError):
        return error
    task = asyncio.current_task()
    if task is not None and task.cancelling() > 0:
        return error
    return ConnectionError(f"MCP server {server_name!r} closed the connection during setup")


class MCPClientManager:
    """Small MCP client facade for configured servers.

    Connections are stateful by default: initialized sessions stay open, so MCP
    servers can keep process/session state and stream server-initiated events.
    """

    def __init__(self, servers: dict[str, MCPServerConfiguration]):
        self._servers = servers
        self._stdio_sessions: dict[str, _StatefulStdioSession] = {}
        self._streamable_sessions: dict[str, _StatefulStreamableHTTPSession] = {}

    @property
    def has_servers(self) -> bool:
        return bool(self._servers)

    def server_names(self) -> list[str]:
        return sorted(self._servers)

    async def start(self) -> None:
        # A single broken MCP server (bad command, wrong runtime, unreachable host) must
        # NOT take down the whole harness on startup — connect each independently and keep
        # going past failures, so the remaining servers' tools are still available. The
        # failed session is dropped; a later reconcile (file watcher) can retry it.
        #
        # "Broken" includes a server that CONNECTS but never answers the initialize
        # handshake (a hung `uvx`/`npx` spawn, a stalled HTTP endpoint): without a bound,
        # `_connect()` awaits forever and the whole server never finishes booting. So each
        # connection is time-boxed; a server that exceeds it is treated like any other
        # failure and skipped, and the app boots without it.
        for name, configuration in self._servers.items():
            if not configuration.stateful:
                continue
            try:
                if configuration.transport == "stdio":
                    connection = self._stdio_sessions.get(name)
                    if connection is None:
                        connection = _StatefulStdioSession(name, configuration)
                        self._stdio_sessions[name] = connection
                    await asyncio.wait_for(connection._connect(), timeout=active_tuning().duration(Limit.MCP_CONNECT_SECONDS))
                elif configuration.transport == "streamable_http":
                    connection = self._streamable_sessions.get(name)
                    if connection is None:
                        connection = _StatefulStreamableHTTPSession(name, configuration)
                        self._streamable_sessions[name] = connection
                    await asyncio.wait_for(connection._connect(), timeout=active_tuning().duration(Limit.MCP_CONNECT_SECONDS))
            except (Exception, asyncio.TimeoutError) as exception:
                logger.warning("MCP server %r failed to start; skipping it: %s", name, exception)
                self._stdio_sessions.pop(name, None)
                self._streamable_sessions.pop(name, None)

    async def reconcile(self, servers: dict[str, MCPServerConfiguration]) -> None:
        """Apply a new server set live, without a full restart. Sessions for
        removed or changed servers are closed; unchanged servers stay connected;
        new (or changed) stateful servers are started. Used by the file watcher so
        editing ``mcp.json`` takes effect with no downtime."""
        for name, configuration in list(self._servers.items()):
            if name not in servers or servers[name] != configuration:
                await self._close_session(name)
        self._servers = dict(servers)
        await self.start()

    async def _close_session(self, name: str) -> None:
        connection = self._stdio_sessions.pop(name, None)
        if connection is not None:
            await self._close_connection(connection)
        connection = self._streamable_sessions.pop(name, None)
        if connection is not None:
            await self._close_connection(connection)

    async def list_tools(self, server: str = "") -> dict[str, Any]:
        """What every selected server offers.

        A server that cannot be reached is listed with no tools rather than failing the
        whole call: one misconfigured or offline endpoint should grey out its own row, not
        take the tool panel — and the agent's tool gating — down with it. Naming a single
        server is a direct question, so that one does raise."""
        result: dict[str, Any] = {"servers": []}
        for name in self._selected_servers(server):
            try:
                async with self._session(name) as session:
                    tools_result = await session.list_tools()
            except Exception as error:  # noqa: BLE001 — an unreachable server is a fact, not a failure
                if server:
                    raise
                logger.warning("Could not list tools for MCP server %s: %s", name, error)
                result["servers"].append({"name": name, "tools": [], "error": str(error)})
                continue
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

    async def call_tool(
        self,
        server: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        event_callback: MCPEventCallback | None = None,
    ) -> dict[str, Any]:
        if not server:
            raise ValueError("server is required when calling an MCP tool")
        async with self._session(server, event_callback=event_callback) as session:
            result = await session.call_tool(
                tool_name,
                arguments or {},
                progress_callback=_progress_callback(server, tool_name, event_callback),
            )
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

    async def aclose(self) -> None:
        connections = [
            *list(self._stdio_sessions.values()),
            *list(self._streamable_sessions.values()),
        ]
        self._stdio_sessions.clear()
        self._streamable_sessions.clear()
        for connection in connections:
            await self._close_connection(connection)

    @staticmethod
    async def _close_connection(connection: Any) -> None:
        try:
            await connection.aclose()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            pass

    async def list_resources(self, server: str = "") -> dict[str, Any]:
        """What every selected server exposes. Tolerant of an unreachable server for the
        same reason `list_tools` is."""
        result: dict[str, Any] = {"servers": []}
        for name in self._selected_servers(server):
            try:
                async with self._session(name) as session:
                    resources_result = await session.list_resources()
            except Exception as error:  # noqa: BLE001
                if server:
                    raise
                logger.warning("Could not list resources for MCP server %s: %s", name, error)
                result["servers"].append({"name": name, "resources": [], "error": str(error)})
                continue
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
            result = await session.read_resource(AnyUrl(uri))
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
    async def _session(
        self,
        server_name: str,
        event_callback: MCPEventCallback | None = None,
    ) -> AsyncIterator[ClientSession]:
        configuration = self._servers.get(server_name)
        if configuration is None:
            raise ValueError(f"Unknown MCP server: {server_name}")
        if configuration.transport == "stdio":
            if not configuration.command:
                raise ValueError(f"MCP server '{server_name}' is missing command")
            if configuration.stateful:
                connection = self._stdio_sessions.get(server_name)
                if connection is None:
                    connection = _StatefulStdioSession(server_name, configuration)
                    self._stdio_sessions[server_name] = connection
                async with connection.session(event_callback) as session:
                    yield session
            else:
                async with _stateless_stdio_session(server_name, configuration, event_callback) as session:
                    yield session
            return
        if configuration.transport == "streamable_http":
            if not configuration.url:
                raise ValueError(f"MCP server '{server_name}' is missing url")
            if configuration.stateful:
                connection = self._streamable_sessions.get(server_name)
                if connection is None:
                    connection = _StatefulStreamableHTTPSession(server_name, configuration)
                    self._streamable_sessions[server_name] = connection
                async with connection.session(event_callback) as session:
                    yield session
            else:
                async with _stateless_streamable_http_session(server_name, configuration, event_callback) as session:
                    yield session
            return
        raise ValueError(f"Unsupported MCP transport for '{server_name}': {configuration.transport}")


class _StatefulStdioSession:
    def __init__(self, server_name: str, configuration: MCPServerConfiguration):
        self._server_name = server_name
        self._configuration = configuration
        self._exit_stack = AsyncExitStack()
        self._session: ClientSession | None = None
        self._connect_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._callbacks: set[MCPEventCallback] = set()
        self._pending_events: list[dict[str, Any]] = []

    @asynccontextmanager
    async def session(self, event_callback: MCPEventCallback | None = None) -> AsyncIterator[ClientSession]:
        # Bounded like the startup connect: an endpoint that accepts the connection and then
        # never completes the handshake would otherwise hold the caller open indefinitely,
        # and a settings panel listing tools is not something to hang a request on.
        session = await asyncio.wait_for(self._connect(), timeout=active_tuning().duration(Limit.MCP_CONNECT_SECONDS))
        async with self._operation_lock:
            if event_callback is not None:
                self._callbacks.add(event_callback)
                await self._flush_pending_events(event_callback)
            try:
                yield session
            finally:
                if event_callback is not None:
                    self._callbacks.discard(event_callback)

    async def _connect(self) -> ClientSession:
        async with self._connect_lock:
            if self._session is not None:
                return self._session
            try:
                read_stream, write_stream = await self._exit_stack.enter_async_context(
                    stdio_client(_stdio_parameters(self._configuration))
                )
                session = await self._exit_stack.enter_async_context(
                    ClientSession(
                        read_stream,
                        write_stream,
                        message_handler=self._handle_message,
                    )
                )
                await session.initialize()
            except BaseException as error:
                # Tear the partially-entered contexts down HERE, in the same task they were
                # entered in, so a failed connection's anyio cancel scopes (stdio_client's
                # subprocess task group) unwind within this task and cannot leak into — and
                # cancel — the caller/lifespan task. Reset the stack so a later retry is clean.
                with suppress(BaseException):
                    await self._exit_stack.aclose()
                self._exit_stack = AsyncExitStack()
                self._session = None
                raise _as_connection_failure(error, self._server_name) from error
            self._session = session
            return session

    async def _handle_message(self, message: Any) -> None:
        event = _event_from_mcp_message(self._server_name, message)
        if event is None:
            return
        callbacks = list(self._callbacks)
        if not callbacks:
            self._pending_events.append(event)
            return
        for callback in callbacks:
            await _emit_callback(callback, event)

    async def _flush_pending_events(self, callback: MCPEventCallback) -> None:
        if not self._pending_events:
            return
        events = self._pending_events
        self._pending_events = []
        for event in events:
            await _emit_callback(callback, event)

    async def aclose(self) -> None:
        self._session = None
        await self._exit_stack.aclose()


class _StatefulStreamableHTTPSession:
    def __init__(self, server_name: str, configuration: MCPServerConfiguration):
        self._server_name = server_name
        self._configuration = configuration
        self._exit_stack = AsyncExitStack()
        self._session: ClientSession | None = None
        self._connect_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._callbacks: set[MCPEventCallback] = set()
        self._pending_events: list[dict[str, Any]] = []

    @asynccontextmanager
    async def session(self, event_callback: MCPEventCallback | None = None) -> AsyncIterator[ClientSession]:
        # Bounded like the startup connect: an endpoint that accepts the connection and then
        # never completes the handshake would otherwise hold the caller open indefinitely,
        # and a settings panel listing tools is not something to hang a request on.
        session = await asyncio.wait_for(self._connect(), timeout=active_tuning().duration(Limit.MCP_CONNECT_SECONDS))
        async with self._operation_lock:
            if event_callback is not None:
                self._callbacks.add(event_callback)
                await self._flush_pending_events(event_callback)
            try:
                yield session
            finally:
                if event_callback is not None:
                    self._callbacks.discard(event_callback)

    async def _connect(self) -> ClientSession:
        async with self._connect_lock:
            if self._session is not None:
                return self._session
            try:
                http_client = await self._exit_stack.enter_async_context(_http_client(self._configuration))
                read_stream, write_stream, _get_session_id = await self._exit_stack.enter_async_context(
                    streamable_http_client(
                        self._configuration.url,
                        http_client=http_client,
                        terminate_on_close=True,
                    )
                )
                session = await self._exit_stack.enter_async_context(
                    ClientSession(
                        read_stream,
                        write_stream,
                        message_handler=self._handle_message,
                    )
                )
                await session.initialize()
            except BaseException as error:
                # Same-task teardown of the partially-entered contexts on failure, so the
                # streamable-http client's anyio task group unwinds within this task instead
                # of leaking its cancel scope into the caller/lifespan task.
                with suppress(BaseException):
                    await self._exit_stack.aclose()
                self._exit_stack = AsyncExitStack()
                self._session = None
                raise _as_connection_failure(error, self._server_name) from error
            self._session = session
            return session

    async def _handle_message(self, message: Any) -> None:
        event = _event_from_mcp_message(self._server_name, message)
        if event is None:
            return
        callbacks = list(self._callbacks)
        if not callbacks:
            self._pending_events.append(event)
            return
        for callback in callbacks:
            await _emit_callback(callback, event)

    async def _flush_pending_events(self, callback: MCPEventCallback) -> None:
        if not self._pending_events:
            return
        events = self._pending_events
        self._pending_events = []
        for event in events:
            await _emit_callback(callback, event)

    async def aclose(self) -> None:
        self._session = None
        await self._exit_stack.aclose()


@asynccontextmanager
async def _stateless_stdio_session(
    server_name: str,
    configuration: MCPServerConfiguration,
    event_callback: MCPEventCallback | None,
) -> AsyncIterator[ClientSession]:
    async with stdio_client(_stdio_parameters(configuration)) as (read_stream, write_stream):
        async with ClientSession(
            read_stream,
            write_stream,
            message_handler=_message_handler(server_name, event_callback),
        ) as session:
            await session.initialize()
            yield session


@asynccontextmanager
async def _stateless_streamable_http_session(
    server_name: str,
    configuration: MCPServerConfiguration,
    event_callback: MCPEventCallback | None,
) -> AsyncIterator[ClientSession]:
    async with _http_client(configuration) as http_client:
        async with streamable_http_client(
            configuration.url,
            http_client=http_client,
            terminate_on_close=True,
        ) as (read_stream, write_stream, _session_id):
            async with ClientSession(
                read_stream,
                write_stream,
                message_handler=_message_handler(server_name, event_callback),
            ) as session:
                await session.initialize()
                yield session


def _stdio_parameters(configuration: MCPServerConfiguration) -> StdioServerParameters:
    return StdioServerParameters(
        command=configuration.command,
        args=configuration.args,
        env=configuration.env or None,
        cwd=str(Path(configuration.cwd).expanduser()) if configuration.cwd else None,
    )


def _http_client(configuration: MCPServerConfiguration) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=configuration.headers or None,
        timeout=httpx.Timeout(configuration.timeout_seconds, read=None),
    )


def _message_handler(server_name: str, event_callback: MCPEventCallback | None):
    async def handle(message: Any) -> None:
        event = _event_from_mcp_message(server_name, message)
        if event is not None and event_callback is not None:
            await _emit_callback(event_callback, event)
    return handle


def _progress_callback(
    server: str,
    tool_name: str,
    event_callback: MCPEventCallback | None,
):
    if event_callback is None:
        return None

    async def progress(progress: float, total: float | None, message: str | None) -> None:
        await _emit_callback(event_callback, {
            "event": "progress",
            "server": server,
            "tool": tool_name,
            "progress": progress,
            "total": total,
            "message": message,
        })

    return progress


async def _emit_callback(callback: MCPEventCallback, event: dict[str, Any]) -> None:
    result = callback(event)
    if result is not None:
        await result


def _event_from_mcp_message(server_name: str, message: Any) -> dict[str, Any] | None:
    if isinstance(message, Exception):
        return {"event": "error", "server": server_name, "message": str(message)}
    if isinstance(message, RequestResponder):
        request = _dump_model(message.request)
        return {
            "event": "server_request",
            "server": server_name,
            "request_id": message.request_id,
            "payload": _strip_render_payloads(request),
        }
    payload = _dump_model(message)
    artifacts = _find_artifacts(payload)
    return {
        "event": "server_notification",
        "server": server_name,
        "payload": _strip_render_payloads(payload),
        "artifacts": artifacts,
    }


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
    if "artifact_id" not in normalized:
        artifact_id = normalized.get("artifactId") or normalized.get("id")
        if isinstance(artifact_id, str) and artifact_id.strip():
            normalized["artifact_id"] = artifact_id.strip()
    if "artifact_target_id" not in normalized:
        target_id = (
            normalized.get("artifactTargetId")
            or normalized.get("target_artifact_id")
            or normalized.get("targetArtifactId")
        )
        if isinstance(target_id, str) and target_id.strip():
            normalized["artifact_target_id"] = target_id.strip()
    if "artifact_update_mode" not in normalized:
        update_mode = (
            normalized.get("artifactUpdateMode")
            or normalized.get("update_mode")
            or normalized.get("updateMode")
        )
        if isinstance(update_mode, str) and update_mode.strip():
            normalized["artifact_update_mode"] = update_mode.strip().lower()
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
                for key in (
                    "artifact_id",
                    "artifact_target_id",
                    "artifact_update_mode",
                    "type",
                    "title",
                    "mime_type",
                    "width",
                    "height",
                    "summary",
                )
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
