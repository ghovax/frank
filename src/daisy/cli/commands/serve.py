"""`daisy serve`: serve the interface over HTTP, so a browser is a client like any other.

The desktop app is a window around a static site that talks to the daemon. Nothing about that
site needs a window — but until now the only way to see it was to install a Tauri application,
which is a strange requirement for a harness whose whole point is that a session is addressable.
On a headless box, or over an SSH tunnel, or simply on a machine where you would rather not
install an app, there was no interface at all.

So this serves the same static export the app embeds, and does one thing more, which is the
part that actually matters: it **proxies** the daemon rather than pointing the browser at it.

Pointing would have been less code. It would also mean handing the daemon's capability token to
a page — a page in a browser full of extensions, whose storage survives the tab — and it would
mean the page had to learn the daemon's port, which is ephemeral and chosen per boot. Proxying
removes both problems at once: the browser talks to this server's own origin, the token is
attached here and never leaves the process, and the ephemeral port is nobody's business but
this file's. It also sidesteps CORS entirely, because there is only one origin involved.

What is proxied is everything that is not a file: ordinary requests, server-sent event streams
(the session transcript), and the terminal's websocket. All three are the interface working
rather than optional extras, so all three are here.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

# Requests whose bodies are streamed rather than buffered, and headers that must not be copied
# between the two hops. `Host` would name this server rather than the daemon; the hop-by-hop
# headers describe a connection that ends here, and forwarding them corrupts the next one.
_DROPPED_REQUEST_HEADERS = frozenset({
    "host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "authorization",
})
_DROPPED_RESPONSE_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-encoding", "content-length",
})

# Served to the page so it knows it is behind this proxy and should address the daemon
# relatively. Without it the bundle falls back to its build-time default — a fixed port the
# daemon does not bind, because it takes an ephemeral one.
RUNTIME_PATH = "/__daisy/runtime.json"


def _note(message: str) -> None:
    print(message, file=sys.stderr)


def interface_directory() -> Optional[Path]:
    """Where the built interface is, or ``None`` if it has not been built.

    Two places, because there are two ways to be running. A frozen build carries the export
    inside itself, next to the other bundled data. A checkout has it wherever `bun run build`
    put it, which is `web/out` relative to the repository root — found by walking up from this
    file rather than by assuming a working directory, so `daisy serve` works from anywhere."""
    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "web"
        return bundled if (bundled / "index.html").is_file() else None
    here = Path(__file__).resolve()
    for candidate in here.parents:
        export = candidate / "web" / "out"
        if (export / "index.html").is_file():
            return export
    return None


def build_application(daemon_url: str, token: str, directory: Path):
    """The ASGI application: the interface at the root, the daemon behind everything else."""
    import httpx
    from starlette.applications import Starlette
    from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
    from starlette.routing import Route, WebSocketRoute

    client = httpx.AsyncClient(base_url=daemon_url, timeout=None, follow_redirects=False)
    root = directory.resolve()

    async def runtime(_request) -> JSONResponse:
        # An empty base is the whole message: address the daemon relative to this origin, which
        # is what makes the proxy invisible to the page.
        return JSONResponse({"apiBase": "", "proxied": True})

    def static_file(path: str) -> Optional[Path]:
        """The exported file a request path names, or ``None`` if it names none.

        Resolved and then checked to be inside the export, so `..` in a URL cannot reach out of
        it. A directory means its `index.html`, which is how a static export serves routes; a
        bare route with no file is not an error here, it is the daemon's."""
        candidate = (root / path.lstrip("/")).resolve()
        if not candidate.is_relative_to(root):
            return None
        if candidate.is_dir():
            candidate = candidate / "index.html"
        return candidate if candidate.is_file() else None

    async def serve_or_proxy(request) -> Response:
        """A real file wins; everything else is the daemon's.

        These cannot be two routes. Mounting a static handler at the root makes it answer every
        path — including the daemon's, which it does not have and would 404 — while putting a
        catch-all proxy first does the same in reverse, which is what it did on the first
        attempt: the interface itself came back as a proxied 404. One handler that looks before
        it forwards is the only ordering that serves both."""
        if request.method in {"GET", "HEAD"}:
            found = static_file(request.url.path)
            if found is not None:
                return FileResponse(found)
        return await proxy(request)

    async def proxy(request) -> Response:
        upstream = request.url.path
        if request.url.query:
            upstream = f"{upstream}?{request.url.query}"
        headers = {
            name: value for name, value in request.headers.items()
            if name.lower() not in _DROPPED_REQUEST_HEADERS
        }
        headers["Authorization"] = f"Bearer {token}"
        outgoing = client.build_request(
            request.method, upstream, headers=headers, content=request.stream(),
        )
        try:
            response = await client.send(outgoing, stream=True)
        except httpx.HTTPError as error:
            return JSONResponse(
                {"error": {"code": "daemon_unreachable", "message": str(error)}}, status_code=502,
            )
        passed = {
            name: value for name, value in response.headers.items()
            if name.lower() not in _DROPPED_RESPONSE_HEADERS
        }
        # Streamed rather than read: `/events` is a server-sent event stream that stays open for
        # the life of a session, and buffering it would mean the transcript never arrives.
        return StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            headers=passed,
            background=_closing(response),
        )

    def _closing(response):
        from starlette.background import BackgroundTask

        return BackgroundTask(response.aclose)

    async def proxy_websocket(websocket) -> None:
        """Relay a websocket both ways.

        The terminal is a websocket, and a handshake cannot carry an Authorization header —
        which is why the daemon also accepts the token as a query parameter. That is the form
        used here, and it never leaves this process either."""
        import asyncio

        import websockets as websockets_client

        query = str(websocket.url.query or "")
        separator = "&" if query else ""
        target = (
            daemon_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
            + websocket.url.path
            + f"?{query}{separator}token={token}"
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

    return Starlette(routes=[
        Route(RUNTIME_PATH, runtime),
        # Named explicitly rather than caught by the wildcard: an ASGI application dispatches
        # websockets by route, so an HTTP catch-all would never see it.
        WebSocketRoute("/terminal", proxy_websocket),
        Route(
            "/{path:path}", serve_or_proxy,
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        ),
    ])


def run(arguments) -> int:
    import uvicorn

    from daisy.base.paths import daemon_port_path, daemon_token_path
    from daisy.cli.client import ensure_daemon

    directory = interface_directory()
    if directory is None:
        _note(
            "daisy: the interface has not been built. Run `cd web && bun run build` in a "
            "checkout, or install the packaged build which carries it."
        )
        return 1

    # A browser with no daemon behind it is a blank screen with a spinner, so start one for the
    # same reason `daisy app` does: the command line owns the daemon, and this is the command
    # line. `--no-daemon` opts out for pointing at something already running.
    if not arguments.no_daemon:
        ensure_daemon()

    try:
        port = int(daemon_port_path().read_text().strip())
        token = daemon_token_path().read_text().strip()
    except (OSError, ValueError):
        _note("daisy: daisyd is not running (start it with `daisy serve`)")
        return 1

    application = build_application(f"http://127.0.0.1:{port}", token, directory)
    address = f"http://{arguments.host}:{arguments.port}"
    _note(f"daisy: serving the interface at {address} (daemon on :{port})")
    _note("daisy: this address carries full control of the daemon — do not expose it beyond loopback.")

    # Opening the browser is what makes this `web` rather than a second `serve`. `serve` is the
    # API alone; this is the API plus the thing that looks at it, and a command that serves an
    # interface and then leaves you to find it is doing half a job. `--no-open` is for a
    # headless box, where there is no browser to open and the printed address is the point.
    if not arguments.no_open:
        _open_when_listening(address)

    uvicorn.run(application, host=arguments.host, port=arguments.port, log_level="warning")
    return 0


def _open_when_listening(address: str) -> None:
    """Open the browser once the server is actually accepting connections.

    Not before: `uvicorn.run` blocks, so opening first races the bind and lands the browser on
    a connection error often enough to matter. A thread that waits for the socket and then
    opens is the smallest thing that is not a race — and it is a daemon thread, so a server
    that never binds does not leave the process unable to exit."""
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

    threading.Thread(target=wait_and_open, name="daisy-web-open", daemon=True).start()
