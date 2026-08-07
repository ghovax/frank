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


# How long to wait for one server's handshake at startup before booting without it.

from langmesh.base.configuration import MCPServerConfiguration
from langmesh.base.tuning import Tunable, active_tuning
from langmesh.base.errors import log_fields

logger = logging.getLogger(__name__)

MCPEventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


def _as_connection_failure(error: BaseException, server_name: str) -> BaseException:
    """Turn a cancellation raised by a failed connect into a real error."""
    if not isinstance(error, asyncio.CancelledError):
        return error
    task = asyncio.current_task()
    if task is not None and task.cancelling() > 0:
        return error
    return ConnectionError(f"MCP server {server_name!r} closed the connection during setup")


class MCPClientManager:
    """A small client facade for configured servers, keeping initialized sessions open by default."""

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
        # One broken server must not take down the harness, so each connects independently.
        for name, configuration in self._servers.items():
            if not configuration.stateful:
                continue
            try:
                if configuration.transport == "stdio":
                    connection = self._stdio_sessions.get(name)
                    if connection is None:
                        connection = _StatefulStdioSession(name, configuration)
                        self._stdio_sessions[name] = connection
                    await asyncio.wait_for(
                        connection._connect(), timeout=active_tuning().duration(Tunable.mcp_connect)
                    )
                elif configuration.transport == "streamable_http":
                    connection = self._streamable_sessions.get(name)
                    if connection is None:
                        connection = _StatefulStreamableHTTPSession(name, configuration)
                        self._streamable_sessions[name] = connection
                    await asyncio.wait_for(
                        connection._connect(), timeout=active_tuning().duration(Tunable.mcp_connect)
                    )
            except (Exception, asyncio.TimeoutError):
                logger.warning("MCP server %r failed to start; skipping it", name, exc_info=True)
                self._stdio_sessions.pop(name, None)
                self._streamable_sessions.pop(name, None)

    async def reconcile(self, servers: dict[str, MCPServerConfiguration]) -> None:
        """Apply a new server set live: close what changed, keep what did not, start what is new."""
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
        """What every selected server offers, listing an unreachable one with no tools rather than failing."""
        result: dict[str, Any] = {"servers": []}
        for name in self._selected_servers(server):
            try:
                async with self._session(name) as session:
                    tools_result = await session.list_tools()
            except Exception as error:  # noqa: BLE001 — an unreachable server is a fact, not a failure
                if server:
                    raise
                logger.warning("MCP server tools unavailable", extra=log_fields(error, server=name))
                result["servers"].append({"name": name, "tools": [], "error": str(error)})
                continue
            result["servers"].append(
                {
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
                }
            )
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
        return {
            "server": server,
            "tool": tool_name,
            "is_error": result.isError,
            "content": [_dump_model(content) for content in result.content],
            "structured_content": result.structuredContent,
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
        """What every selected server exposes, tolerant of an unreachable one for the same reason."""
        result: dict[str, Any] = {"servers": []}
        for name in self._selected_servers(server):
            try:
                async with self._session(name) as session:
                    resources_result = await session.list_resources()
            except Exception as error:  # noqa: BLE001
                if server:
                    raise
                logger.warning(
                    "MCP server resources unavailable", extra=log_fields(error, server=name)
                )
                result["servers"].append({"name": name, "resources": [], "error": str(error)})
                continue
            result["servers"].append(
                {
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
                }
            )
        return result

    async def read_resource(self, server: str, uri: str) -> dict[str, Any]:
        if not server:
            raise ValueError("server is required when reading an MCP resource")
        async with self._session(server) as session:
            result = await session.read_resource(AnyUrl(uri))
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
                async with _stateless_stdio_session(
                    server_name, configuration, event_callback
                ) as session:
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
                async with _stateless_streamable_http_session(
                    server_name, configuration, event_callback
                ) as session:
                    yield session
            return
        raise ValueError(
            f"Unsupported MCP transport for '{server_name}': {configuration.transport}"
        )


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
    async def session(
        self, event_callback: MCPEventCallback | None = None
    ) -> AsyncIterator[ClientSession]:
        # Bounded like the startup connect, so an endpoint that never completes its handshake cannot hold the caller.
        session = await asyncio.wait_for(
            self._connect(), timeout=active_tuning().duration(Tunable.mcp_connect)
        )
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
                # Tear the partially-entered contexts down in the task they were entered in, so their cancel scopes unwind here.
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
    async def session(
        self, event_callback: MCPEventCallback | None = None
    ) -> AsyncIterator[ClientSession]:
        # Bounded like the startup connect, so an endpoint that never completes its handshake cannot hold the caller.
        session = await asyncio.wait_for(
            self._connect(), timeout=active_tuning().duration(Tunable.mcp_connect)
        )
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
                http_client = await self._exit_stack.enter_async_context(
                    _http_client(self._configuration)
                )
                (
                    read_stream,
                    write_stream,
                    _get_session_id,
                ) = await self._exit_stack.enter_async_context(
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
                # Same-task teardown on failure, so the client's task group unwinds here rather than leaking its cancel scope.
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
        await _emit_callback(
            event_callback,
            {
                "event": "progress",
                "server": server,
                "tool": tool_name,
                "progress": progress,
                "total": total,
                "message": message,
            },
        )

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
            "payload": request,
        }
    return {
        "event": "server_notification",
        "server": server_name,
        "payload": _dump_model(message),
    }


def _dump_model(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)
