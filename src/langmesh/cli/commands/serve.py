"""`langmesh serve`: serve the interface over HTTP, so a browser is a client like any other."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from pathlib import Path
from typing import Callable, Optional

# Requests whose bodies are streamed, and headers that describe a connection ending here rather than at the daemon.
_DROPPED_REQUEST_HEADERS = frozenset(
    {
        "host",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "authorization",
    }
)
# `content-encoding` is deliberately kept, since the body is forwarded exactly as it arrived and still compressed.
_DROPPED_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",
    }
)

# Served to the page so it addresses the daemon relatively rather than at a build-time default port.
RUNTIME_PATH = "/__langmesh/runtime.json"


logger = logging.getLogger("langmesh.serve")

# Requests this proxy may send a second time, being the ones that carry no one-shot body.
_REPLAYABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})


def interface_directory() -> Optional[Path]:
    """Where the built interface is, or `None`: inside a frozen build, or wherever a checkout put its export."""
    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "web"
        return bundled if (bundled / "index.html").is_file() else None
    here = Path(__file__).resolve()
    for candidate in here.parents:
        export = candidate / "web" / "out"
        if (export / "index.html").is_file():
            return export
    return None


# What belongs to the interface rather than the daemon, consulted only when the interface is a development server.
_INTERFACE_PREFIXES = ("/_next/", "/__next", "/fonts/", "/@vite", "/@react-refresh")
_INTERFACE_PATHS = frozenset(
    {
        "/",
        "/favicon.ico",
        "/icon.png",
        "/apple-icon.png",
        "/manifest.json",
        "/dictation-capture.worklet.js",
    }
)


def _wants_interface(path: str) -> bool:
    return path in _INTERFACE_PATHS or path.startswith(_INTERFACE_PREFIXES)


def build_application(
    daemon_url: str,
    token: str,
    directory: Optional[Path],
    interface_url: str = "",
    rediscover: Optional[Callable[[], tuple[str, str]]] = None,
):
    """The ASGI application: the interface at the root, the daemon behind everything else, rediscovered when it moves."""
    import httpx
    from starlette.applications import Starlette
    from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
    from starlette.routing import Route, WebSocketRoute

    # Where the daemon is *now*. Mutable because it moves: see `rediscover` above.
    upstream_daemon = {"url": daemon_url, "token": token}
    client = httpx.AsyncClient(base_url=daemon_url, timeout=None, follow_redirects=False)

    async def find_daemon_again() -> bool:
        """Re-read where the daemon is. True when it moved, which is when a retry is worth it."""
        nonlocal client
        if rediscover is None:
            return False
        try:
            found_url, found_token = rediscover()
        except Exception:  # noqa: BLE001 — a proxy must not die because a file was mid-write
            logger.debug("could not re-read the daemon's endpoint", exc_info=True)
            return False
        if not found_url or (
            found_url == upstream_daemon["url"] and found_token == upstream_daemon["token"]
        ):
            return False
        logger.info(f"langmesh: the daemon moved to {found_url}; reconnecting.")
        upstream_daemon["url"] = found_url
        upstream_daemon["token"] = found_token
        previous, client = (
            client,
            httpx.AsyncClient(base_url=found_url, timeout=None, follow_redirects=False),
        )
        await previous.aclose()
        return True

    interface = (
        httpx.AsyncClient(base_url=interface_url, timeout=None, follow_redirects=False)
        if interface_url
        else None
    )
    root = directory.resolve() if directory is not None else None

    async def runtime(_request) -> JSONResponse:
        # An empty base is the whole message: address the daemon relative to this origin.
        return JSONResponse({"apiBase": "", "proxied": True})

    def static_file(path: str) -> Optional[Path]:
        """The exported file a request path names, resolved and checked to be inside the export so `..` cannot escape."""
        if root is None:
            return None
        candidate = (root / path.lstrip("/")).resolve()
        if not candidate.is_relative_to(root):
            return None
        if candidate.is_dir():
            candidate = candidate / "index.html"
        return candidate if candidate.is_file() else None

    async def serve_or_proxy(request) -> Response:
        """A real file wins and everything else is the daemon's, which cannot be two routes."""
        if interface is not None and _wants_interface(request.url.path):
            return await proxy(request, interface, authorise=False)
        if request.method in {"GET", "HEAD"}:
            found = static_file(request.url.path)
            if found is not None:
                return FileResponse(found)
        return await proxy(request)

    async def proxy(request, upstream_client=None, authorise: bool = True) -> Response:
        # Resolved per call rather than captured, so a reconnection is picked up by everything that follows.
        to_daemon = upstream_client is None
        upstream_client = upstream_client or client
        upstream = request.url.path
        if request.url.query:
            upstream = f"{upstream}?{request.url.query}"
        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in _DROPPED_REQUEST_HEADERS
        }
        # Ask the upstream for exactly what the caller asked us for, rather than letting httpx add its own encodings.
        headers.setdefault("accept-encoding", "identity")

        # The daemon needs the capability token and a development server must not be handed one.
        if authorise:
            headers["Authorization"] = f"Bearer {upstream_daemon['token']}"
        outgoing = upstream_client.build_request(
            request.method,
            upstream,
            headers=headers,
            content=request.stream(),
        )
        try:
            response = await upstream_client.send(outgoing, stream=True)
        except httpx.ConnectError as error:
            # Nothing is listening there: either the daemon is down or it moved, and those are told apart by looking.
            moved = to_daemon and await find_daemon_again()
            if not moved:
                return JSONResponse(
                    {"error": {"code": "daemon_unreachable", "message": str(error)}},
                    status_code=502,
                )
            # Retried only when this request's body can be produced again, since a stream re-sent would forward nothing.
            if request.method.upper() not in _REPLAYABLE_METHODS:
                return JSONResponse(
                    {
                        "error": {
                            "code": "daemon_moved",
                            "message": "The daemon restarted; try that again.",
                        }
                    },
                    status_code=503,
                )
            headers["Authorization"] = f"Bearer {upstream_daemon['token']}"
            retried = client.build_request(request.method, upstream, headers=headers)
            try:
                response = await client.send(retried, stream=True)
            except httpx.HTTPError as retry_error:
                return JSONResponse(
                    {"error": {"code": "daemon_unreachable", "message": str(retry_error)}},
                    status_code=502,
                )
        except httpx.HTTPError as error:
            return JSONResponse(
                {"error": {"code": "daemon_unreachable", "message": str(error)}},
                status_code=502,
            )
        passed = {
            name: value
            for name, value in response.headers.items()
            if name.lower() not in _DROPPED_RESPONSE_HEADERS
        }
        # Streamed rather than read, because the event stream stays open for the life of a session.
        return StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            headers=passed,
            background=_closing(response),
        )

    def _closing(response):
        from starlette.background import BackgroundTask

        return BackgroundTask(response.aclose)

    async def proxy_interface_websocket(websocket) -> None:
        """The development server's hot-reload socket, relayed like the terminal's but without the token."""
        if interface is None:
            await websocket.close(code=1008)
            return
        await _relay(websocket, interface_url, append_token=False)

    async def proxy_websocket(websocket) -> None:
        """Relay a websocket both ways, passing the token as a query parameter since a handshake carries no header."""

        # Read at connect time, so a terminal opened after the daemon moved reaches the daemon rather than its old port.
        await _relay(websocket, upstream_daemon["url"], append_token=True)

    async def _relay(websocket, base: str, append_token: bool) -> None:
        import websockets as websockets_client

        query = str(websocket.url.query or "")
        if append_token:
            separator = "&" if query else ""
            query = f"{query}{separator}token={upstream_daemon['token']}"
        target = (
            base.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
            + websocket.url.path
            + (f"?{query}" if query else "")
        )
        await websocket.accept()
        try:
            async with websockets_client.connect(target, open_timeout=20) as upstream:

                async def downstream_to_upstream() -> None:
                    while True:
                        message = await websocket.receive()
                        if message.get("type") == "websocket.disconnect":
                            return
                        if (text := message.get("text")) is not None:
                            await upstream.send(text)
                        elif (data := message.get("bytes")) is not None:
                            await upstream.send(data)

                async def upstream_to_downstream() -> None:
                    async for frame in upstream:
                        if isinstance(frame, bytes):
                            await websocket.send_bytes(frame)
                        else:
                            await websocket.send_text(frame)

                first, pending = await asyncio.wait(
                    [
                        asyncio.create_task(downstream_to_upstream()),
                        asyncio.create_task(upstream_to_downstream()),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                for task in first:
                    task.exception()
        except Exception:  # noqa: BLE001 — a dropped relay closes the socket, it does not raise
            pass
        finally:
            try:
                await websocket.close()
            except RuntimeError:
                # Already closed by whichever side went first.
                pass

    return Starlette(
        routes=[
            Route(RUNTIME_PATH, runtime),
            # Named explicitly rather than caught by the wildcard, since an HTTP catch-all never sees a websocket route.
            WebSocketRoute("/terminal", proxy_websocket),
            # The development server's hot-reload channel, which is what lets a change reach the phone without a rebuild.
            WebSocketRoute("/_next/{path:path}", proxy_interface_websocket),
            Route(
                "/{path:path}",
                serve_or_proxy,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
            ),
        ]
    )


def _port_is_taken(host: str, port: int) -> bool:
    """Whether something already listens there. Asked before a daemon is started, not after."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return True
    return False


def run(arguments) -> int:
    import uvicorn

    import os
    import signal

    from langmesh.base.paths import daemon_port_path, daemon_token_path, runtime_directory
    from langmesh.cli.client import daemon_is_up, ensure_daemon

    directory = interface_directory()
    if directory is None:
        logger.info(
            "langmesh: the interface has not been built. Run `cd web && bun run build` in a checkout, or install the packaged build which carries it."
        )
        return 1

    # Claim the port before starting anything, so a failed bind does not leave a daemon nobody asked for.
    if _port_is_taken(arguments.host, arguments.port):
        logger.info(
            f"langmesh: {arguments.host}:{arguments.port} is already in use — most likely another `langmesh serve`. Stop it, or pass `--port` to use a different one."
        )
        return 1

    # A browser with no daemon behind it is a blank screen, so one is started unconditionally.
    started_the_daemon = not daemon_is_up()
    ensure_daemon()

    def stop_daemon_if_started() -> None:
        """Undo our own side effect. A daemon someone else was already running is left alone."""
        if not started_the_daemon:
            return
        try:
            pid = int((runtime_directory() / "langmeshd.pid").read_text().strip())
        except (OSError, ValueError):
            return
        # The group, so the sessions go with it, by the same reasoning and the same signal as `langmesh daemon stop`.
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        logger.info("langmesh: stopped the daemon this command had started.")

    try:
        port = int(daemon_port_path().read_text().strip())
        token = daemon_token_path().read_text().strip()
    except (OSError, ValueError):
        logger.info("langmesh: langmeshd is not running (start it with `langmesh serve`)")
        stop_daemon_if_started()
        return 1

    application = build_application(f"http://127.0.0.1:{port}", token, directory)
    address = f"http://{arguments.host}:{arguments.port}"
    logger.info(f"langmesh: serving the interface at {address} (daemon on :{port})")
    logger.info(
        "langmesh: this address carries full control of the daemon — do not expose it beyond loopback."
    )

    # Asked for, never assumed: serving and opening a window are two different acts.
    if arguments.open_browser:
        _open_when_listening(address)

    try:
        configuration = uvicorn.Config(
            application,
            host=arguments.host,
            port=arguments.port,
            log_level="warning",
            # This server holds connections that never end on their own, so shutdown needs a deadline or Ctrl-C hangs.
        )
        # SIGTERM needs a handler of its own, because uvicorn restores and re-raises rather than exiting.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        uvicorn.Server(configuration).run()
    finally:
        # `finally` rather than `except`, since uvicorn returns normally when it is not re-raising.
        stop_daemon_if_started()
    return 0


def _open_when_listening(address: str) -> None:
    """Open the browser once the server is accepting connections, since opening first races the bind."""
    import socket
    import threading
    import time
    import urllib.parse
    import webbrowser

    parsed = urllib.parse.urlparse(address)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 80

    def wait_and_open() -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                probe.settimeout(0.25)
                if probe.connect_ex((host, port)) == 0:
                    break
            time.sleep(0.1)
        else:
            return
        try:
            webbrowser.open(address)
        except Exception:  # noqa: BLE001 — no browser is not an error, the address is printed
            pass

    threading.Thread(target=wait_and_open, name="langmesh-web-open", daemon=True).start()
