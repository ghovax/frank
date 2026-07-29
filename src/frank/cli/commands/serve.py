"""`frank serve`: serve the interface over HTTP, so a browser is a client like any other.

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
RUNTIME_PATH = "/__frank/runtime.json"


def _note(message: str) -> None:
    print(message, file=sys.stderr)


def interface_directory() -> Optional[Path]:
    """Where the built interface is, or ``None`` if it has not been built.

    Two places, because there are two ways to be running. A frozen build carries the export
    inside itself, next to the other bundled data. A checkout has it wherever `bun run build`
    put it, which is `web/out` relative to the repository root — found by walking up from this
    file rather than by assuming a working directory, so `frank serve` works from anywhere."""
    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "web"
        return bundled if (bundled / "index.html").is_file() else None
    here = Path(__file__).resolve()
    for candidate in here.parents:
        export = candidate / "web" / "out"
        if (export / "index.html").is_file():
            return export
    return None


# Everything the export is built from. `web/src` is the bulk of it, but a translation, a Next
# setting or a dependency bump changes the built output just as surely and would otherwise pass
# the check unnoticed.
INTERFACE_SOURCES = ("src", "messages", "next.config.ts", "package.json")


def stale_interface(directory: Path) -> Optional[tuple[float, float, Path]]:
    """``(built, newest, culprit)`` when the export predates its sources, else ``None``.

    `frank serve` hands out a static export, and nothing regenerates it: `web/out` is gitignored,
    so a checkout neither builds it nor updates it on a pull. The only condition ever checked was
    that `index.html` exists, which a months-old build satisfies exactly as well as a fresh one.

    The failure that produces is the worst kind available. The interface loads, it looks right,
    and it runs code nobody has touched in a day — so a bug fixed hours ago is reported as still
    broken, re-diagnosed from nothing, and "fixed" a second time. That happened here: a Jul 28
    21:00 export was served against a source tree from Jul 29 14:23, and two separate defects
    already repaired in `web/src` were investigated again from scratch because the browser was
    running the version from before the repair.

    Checkout-only. A frozen build carries its own bundle, built by the packaging script moments
    before freezing, and has no source tree to compare it against."""
    if getattr(sys, "frozen", False):
        return None
    index = directory / "index.html"
    if not index.is_file():
        return None
    built = index.stat().st_mtime
    newest, culprit = 0.0, index
    for entry in INTERFACE_SOURCES:
        source = directory.parent / entry
        candidates = source.rglob("*") if source.is_dir() else ([source] if source.is_file() else [])
        for candidate in candidates:
            if not candidate.is_file():
                continue
            modified = candidate.stat().st_mtime
            if modified > newest:
                newest, culprit = modified, candidate
    if newest <= built:
        return None
    return built, newest, culprit


def describe_stale_interface(built: float, newest: float, culprit: Path, directory: Path) -> str:
    """The message a person needs: what is being served, what is newer, and what to do.

    Names the specific file rather than the directory, because "something under web/src changed"
    sends you looking while "web/src/lib/use-chat.ts is newer" does not."""
    from datetime import datetime

    try:
        where = culprit.relative_to(directory.parent.parent)
    except ValueError:
        where = culprit
    return (
        f"frank: the built interface is older than its sources, so the browser would run code "
        f"that no longer matches this checkout.\n"
        f"       serving : {directory} (built {datetime.fromtimestamp(built):%Y-%m-%d %H:%M})\n"
        f"       newer   : {where} ({datetime.fromtimestamp(newest):%Y-%m-%d %H:%M})\n"
        f"       Run `cd web && bun run build` to rebuild it, or pass --allow-stale-interface "
        f"to serve the old build anyway."
    )


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

    import contextlib
    import os
    import signal

    from frank.base.paths import daemon_port_path, daemon_token_path, runtime_directory
    from frank.cli.client import daemon_is_up, ensure_daemon

    directory = interface_directory()
    if directory is None:
        _note(
            "frank: the interface has not been built. Run `cd web && bun run build` in a "
            "checkout, or install the packaged build which carries it."
        )
        return 1

    # Refused rather than warned, because a warning is what the previous version of this check
    # would have been, and the whole lesson here is that nobody reads a line of stderr scrolling
    # past a server that started successfully. Serving a stale interface is not a degraded mode,
    # it is showing someone a different program than the one they are editing.
    stale = stale_interface(directory)
    if stale is not None:
        _note(describe_stale_interface(*stale, directory))
        if not getattr(arguments, "allow_stale_interface", False):
            return 1
        _note("frank: serving the stale build anyway, because --allow-stale-interface was passed.")

    # Claim the port before starting anything, because `uvicorn.run` binds last and a bind that
    # fails after `ensure_daemon` leaves a daemon running that nobody asked for and nothing is
    # serving. Whoever already holds the port is almost always an earlier `frank serve`, and the
    # useful thing to say is so — not a traceback from deep inside uvicorn.
    if _port_is_taken(arguments.host, arguments.port):
        _note(
            f"frank: {arguments.host}:{arguments.port} is already in use — most likely another "
            f"`frank serve`. Stop it, or pass `--port` to use a different one."
        )
        return 1

    # A browser with no daemon behind it is a blank screen with a spinner, so start one for the
    # same reason `frank app` does: the command line owns the daemon, and this is the command
    # line. `--no-daemon` opts out for pointing at something already running.
    # Whether *this* command started the daemon. It matters because of what happens if the rest
    # of this function fails: a daemon we started and then never served is a process nobody asked
    # for and nothing is talking to, left behind by a command that reported an error. Claiming the
    # port first removed the likeliest cause; owning what we start removes the rest.
    started_the_daemon = False
    if not arguments.no_daemon:
        started_the_daemon = not daemon_is_up()
        ensure_daemon()

    def stop_the_daemon_we_started() -> None:
        """Undo our own side effect. A daemon someone else was already running is left alone."""
        if not started_the_daemon:
            return
        try:
            pid = int((runtime_directory() / "frankd.pid").read_text().strip())
        except (OSError, ValueError):
            return
        # The group, so the sessions go with it: a worker whose daemon is gone cannot persist
        # anything. The same reasoning, and the same signal, as `frank daemon stop`.
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        _note("frank: stopped the daemon this command had started.")

    try:
        port = int(daemon_port_path().read_text().strip())
        token = daemon_token_path().read_text().strip()
    except (OSError, ValueError):
        _note("frank: frankd is not running (start it with `frank serve`)")
        stop_the_daemon_we_started()
        return 1

    application = build_application(f"http://127.0.0.1:{port}", token, directory)
    address = f"http://{arguments.host}:{arguments.port}"
    _note(f"frank: serving the interface at {address} (daemon on :{port})")
    _note("frank: this address carries full control of the daemon — do not expose it beyond loopback.")

    # Opening the browser is what makes this `web` rather than a second `serve`. `serve` is the
    # API alone; this is the API plus the thing that looks at it, and a command that serves an
    # interface and then leaves you to find it is doing half a job. `--no-open` is for a
    # headless box, where there is no browser to open and the printed address is the point.
    if not arguments.no_open:
        _open_when_listening(address)

    try:
        uvicorn.run(application, host=arguments.host, port=arguments.port, log_level="warning")
    except BaseException:
        # Anything at all — a bind that raced, a keyboard interrupt during startup, a crash in
        # the server. If we started the daemon and never served it, it does not outlive us.
        stop_the_daemon_we_started()
        raise
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

    threading.Thread(target=wait_and_open, name="frank-web-open", daemon=True).start()
