"""`frankd`: the control plane.

It owns the registry, the prototype, the databases, and the shared brokers, and it serves
one API two ways — a unix socket for the CLI and for sessions, and a loopback TCP port for
the desktop client, which cannot open a socket. Both require the capability token written
beside them in the runtime directory.

The daemon runs no agents, and it never imports the runtime. That is what keeps this side
small — and it is also why the *prototype* is a process rather than a function here: the thing
that forks a session has to be the thing that has already paid for the runtime import, and the
daemon must never be that.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import secrets
import signal
import socket
import sys

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketClose
from tenacity import AsyncRetrying, RetryError, retry_if_exception_type, stop_after_delay, wait_fixed

from frank.base.paths import (
    daemon_port_path,
    daemon_socket_path,
    daemon_token_path,
    log_file_path,
    runtime_directory,
)
from frank.base.tuning import Tunable, active_tuning

logger = logging.getLogger("frank.daemon")

# Bound to loopback only. The token is what authorises a call; the bind is what keeps the
# surface off the network entirely.
LOOPBACK_HOST = "127.0.0.1"

# The only browsers with any business here: the desktop app's own webview (whose origin is
# `tauri://localhost` or `http://tauri.localhost` by platform) and a local dev server.
_APP_ORIGIN_PATTERN = (
    r"^(tauri://localhost|https?://tauri\.localhost"
    r"|https?://localhost(:\d+)?|https?://127\.0\.0\.1(:\d+)?)$"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK_HOST, 0))
        return probe.getsockname()[1]


def _write_handshake(token: str, port: int) -> None:
    """Publish where the daemon is and what proves you may talk to it.

    Both files are 0600 in a 0700 directory: on a shared machine, file permissions are the
    access control, so a token another user could read would be no token at all."""
    token_path = daemon_token_path()
    token_path.write_text(token)
    token_path.chmod(0o600)
    port_path = daemon_port_path()
    port_path.write_text(str(port))
    port_path.chmod(0o600)
    # The pid is how `frank daemon stop` reaches a daemon that has stopped answering.
    pidfile = daemon_socket_path().parent / "frankd.pid"
    pidfile.write_text(str(os.getpid()))
    pidfile.chmod(0o600)


def _clear_handshake() -> None:
    for path in (
        daemon_token_path(),
        daemon_port_path(),
        daemon_socket_path(),
        daemon_socket_path().parent / "frankd.pid",
    ):
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def _acquire_singleton_lock() -> int | None:
    """An exclusive, process-lifetime lock: exactly one daemon per user.

    The socket alone cannot enforce this. Two `frank` commands run at the same moment both find
    no daemon, both start one, and uvicorn unlinks an existing unix socket before binding — so
    the second daemon silently takes the socket from the first and the first becomes an orphan
    supervising sessions nothing will ever reap. An advisory lock is decided by the kernel
    rather than by who checked first, which is what closes the window between looking and
    binding.

    The descriptor is deliberately never closed: the lock lives exactly as long as the process
    that holds it, and the kernel drops it even if the daemon is killed outright."""
    path = runtime_directory() / "frankd.lock"
    handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(handle)
        return None
    return handle


async def _defer_to_running_daemon() -> int:
    """Stand down for the daemon that won the lock, once it is actually serving.

    Reporting ready is the point: whoever started this process is waiting on that one line
    before it sends its first command, and the daemon it will reach is the other one. Exiting
    silently instead would fail a command that has a perfectly good daemon to talk to.

    This waits by connecting, unlike everything else in the daemon, because the thing being
    waited on is in another process — there is no event to await across that boundary, only the
    socket itself becoming answerable. The waiting itself is tenacity's, so what is left here is
    the probe and what to do once it answers; the interval and the deadline are policy settings
    like every other duration in the harness."""
    path = daemon_socket_path()
    tuning = active_tuning()

    def probe_once() -> None:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(tuning.duration(Tunable.daemon_probe_connect_seconds))
        try:
            probe.connect(str(path))
        finally:
            probe.close()

    try:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(OSError),
            wait=wait_fixed(tuning.duration(Tunable.daemon_probe_interval_seconds)),
            stop=stop_after_delay(tuning.duration(Tunable.daemon_startup_seconds)),
        ):
            with attempt:
                await asyncio.to_thread(probe_once)
    except RetryError:
        logger.error("another frankd holds the lock but never started serving")
        return 1
    with contextlib.suppress(OSError, ValueError):
        sys.stdout.write(json.dumps({"ready": True, "deferred": True}) + "\n")
        sys.stdout.flush()
        sys.stdout.close()
    logger.info("another frankd already holds the runtime directory, standing down")
    return 0


def _reclaim_socket() -> None:
    """Remove a socket left behind by a daemon that died without cleaning up.

    A stale socket file is indistinguishable from a live one by existence alone, so the test
    is whether anything is actually listening: connect, and if the connection is refused the
    file is a corpse and can be removed. Getting this wrong in either direction is bad — a
    false positive kills a running daemon's socket, a false negative makes startup fail
    forever — which is why it is a connect and not a `path.exists()`."""
    path = daemon_socket_path()
    if not path.exists():
        return
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        return
    finally:
        probe.close()
    raise SystemExit(f"A daemon is already listening on {path}.")


def _announcing_server_class():
    """Built on demand so importing this module does not pull in uvicorn."""
    import uvicorn

    class AnnouncingServer(uvicorn.Server):
        """A uvicorn server that sets an event once it is accepting connections.

        uvicorn exposes readiness only as a `started` attribute, which leaves callers spinning
        on it. Overriding `startup` makes readiness awaitable, so nothing polls, and a server
        that fails to bind surfaces as a failed task rather than a loop that never ends."""

        def __init__(self, config) -> None:  # noqa: ANN001 — matches uvicorn's signature
            super().__init__(config)
            self.ready = asyncio.Event()

        async def startup(self, sockets=None) -> None:  # noqa: ANN001
            await super().startup(sockets=sockets)
            self.ready.set()

    return AnnouncingServer


def build_app() -> FastAPI:
    from frank.daemon import state
    from frank.daemon.api import RpcError
    from frank.daemon.api import router as control_router
    from frank.daemon.ingest import router as ingest_router
    from frank.daemon.peer_identity import calling_session
    from frank.rest.app import mount as mount_gui_routes

    app = FastAPI(title="frankd")

    @app.exception_handler(RpcError)
    async def _rpc_error(_request: Request, error: RpcError) -> JSONResponse:
        """The same error shape whether it was raised under `/rpc` or under a plain route.

        `/rpc` catches these itself; the streaming routes do not, and without this an
        `attach` to a session that does not exist came back as an opaque 500 with the
        reason — the one useful part — swallowed by the default handler."""
        return JSONResponse(
            {"error": {"code": error.code, "message": error.message}}, status_code=error.status_code
        )

    # Pure ASGI, not `@app.middleware("http")`. That decorator builds a `BaseHTTPMiddleware`,
    # which runs the rest of the app inside an anyio task group and pumps the response body
    # through a memory stream — and this daemon's busiest routes are streams: `attach` and
    # `/events` are open for the life of a page. When a client goes away mid-stream, which a
    # browser does on every reload, the task group unwinds from a different task than the one
    # that entered it and the request dies with "Attempted to exit a cancel scope that isn't
    # the current task's current cancel scope". The reply never arrives, and a caller that was
    # only submitting an approval is told its request is no longer active.
    #
    # Reading the scope and calling the next app directly has no task group and so no scope to
    # mis-exit. Nothing about the policy below changed.
    class Authenticate:
        def __init__(self, application):
            self.application = application

        async def __call__(self, scope, receive, send):
            # Websockets are checked here too, and that is not a widening of the policy — it is
            # the policy finally being applied where it was always meant to. This read
            # `scope["type"] != "http"`, which passed every handshake straight through; the only
            # websocket route is `/terminal`, which is a PTY, or an SSH login shell to a
            # configured host. Anything that could open a TCP connection to the loopback port got
            # a shell without presenting anything. Clients were already sending the token — the
            # query parameter below exists for exactly this transport — and nothing read it.
            #
            # `lifespan`, and whatever else a server invents, is not a request and carries no
            # credential to check.
            if scope["type"] not in {"http", "websocket"}:
                return await self.application(scope, receive, send)

            # `HTTPConnection` rather than `Request`: a websocket scope is not a request and
            # `Request` will not take one. What is read below — the path, the headers, the query
            # string — is common to both, and is precisely what `HTTPConnection` is for.
            connection = HTTPConnection(scope)

            if scope["type"] == "http":
                if connection.url.path in {"/health"}:
                    return await self.application(scope, receive, send)
                # A CORS preflight, which a browser sends without credentials by specification and
                # which asks only whether the real request may follow. Demanding a token here
                # rejects the question rather than the request, and the real one is then never
                # sent at all.
                #
                # The middleware order already keeps these from reaching this point — CORS is
                # outermost and answers them itself — so this is the belt to that pair of braces:
                # the failure it prevents is silent, remote from its cause, and was diagnosed once
                # already.
                #
                # HTTP only, and there is nothing to add for websockets: a handshake is not
                # preflighted, and there is no unauthenticated websocket path to exempt.
                if scope.get("method") == "OPTIONS" and "access-control-request-method" in connection.headers:
                    return await self.application(scope, receive, send)

            header = connection.headers.get("Authorization", "")
            # A WebSocket handshake and an <img>/<iframe> URL cannot carry a header, so the
            # terminal passes the same token as a query parameter.
            presented = (
                header[len("Bearer "):] if header.startswith("Bearer ")
                else connection.query_params.get("token", "")
            )
            # Who the *kernel* says is calling, which no token can contradict. A session's own
            # shell can read the daemon's 0600 token file — same user, same machine — so a token
            # alone could never establish that a caller was not a session. This can.
            peer_session = calling_session(scope)
            scope.setdefault("state", {})

            if presented and secrets.compare_digest(presented, state.daemon_token):
                # The daemon token says "you may drive this daemon" and nothing about who is
                # asking — so a caller the kernel identified as a session is still that session,
                # holding this token or not. That is what closes the escape hatch: reading the
                # token buys a session no anonymity.
                scope["state"]["calling_session"] = peer_session or ""
                return await self.application(scope, receive, send)
            # A session's own token identifies *which* session is calling, which the daemon token
            # cannot. That attribution is what lets a session's control-plane calls be attributed
            # to it — so a peer it creates is its child, and is clamped against it, whatever the
            # request body claims.
            caller = state.registry.session_for_token(presented) if (presented and state.registry) else None
            if caller is None:
                return await self._refuse(scope, receive, send)
            # The kernel's answer wins over the token's when both are available: a session holding
            # another session's token is still itself.
            scope["state"]["calling_session"] = peer_session or caller.id
            return await self.application(scope, receive, send)

        @staticmethod
        async def _refuse(scope, receive, send) -> None:
            """Say no in the shape the caller's transport understands.

            A websocket is closed *without being accepted*, which is what ASGI calls refusing a
            handshake and what the server turns into an HTTP failure. Accepting first and closing
            after would look like a connection that was allowed and then dropped — and would give
            the route a moment in which it believed it had a client."""
            if scope["type"] == "websocket":
                # 1008 is "policy violation", which is the closest the protocol has to a 401.
                return await WebSocketClose(code=1008)(scope, receive, send)
            response = JSONResponse(
                {"error": {"code": "unauthorized", "message": "Bad or missing token."}},
                status_code=401,
            )
            return await response(scope, receive, send)

    app.add_middleware(Authenticate)
    # Added *after* `Authenticate`, and the order is the whole point: `add_middleware` prepends,
    # so the last one added is the outermost, and this has to be outside the token check.
    #
    # It was inside it, and the desktop app could not read anything. A browser sends a CORS
    # preflight before any request carrying a non-simple header — `Authorization` is one — and it
    # sends that preflight *without* credentials, by specification. So the preflight arrived with
    # no token, the token check answered 401, and the real request was never sent: every list in
    # the app was empty, every write failed, and none of it looked like an authentication problem
    # because the 401 was invisible. The same build served by `frank web` worked perfectly, since
    # a same-origin page preflights nothing — which is exactly what made this look like a fault
    # in the app rather than in the daemon.
    #
    # Outermost also means the CORS headers are attached to *failures*. A 401 raised in the
    # middleware below carried none, so the webview could not read the status and reported an
    # opaque network error in place of the reason.
    #
    # Scoped to the app's own origins — reflecting `*` would let any page the user happened to
    # visit script a local, tool-executing API.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_APP_ORIGIN_PATTERN,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict:
        """Unauthenticated on purpose: a client needs to know the daemon is up before it has
        read the token, and this says nothing else."""
        return {"ok": True, "service": "frankd"}

    app.include_router(control_router)
    app.include_router(ingest_router)
    mount_gui_routes(app)
    return app


async def _serve() -> int:
    import uvicorn

    from frank.base import confinement
    from frank.base.background_store import reap_orphaned_process_groups
    from frank.base.configuration import Configuration
    from frank.daemon import state
    from frank.hub import state as hub_state
    from frank.daemon.composition import close_shared_resources, open_shared_resources
    from frank.daemon.lifecycle import SessionLifecycle
    from frank.daemon.peer_identity import unix_peer_protocol
    from frank.daemon.prototype import PrototypeClient
    from frank.daemon.persistence.sessions import SqliteSessionStore
    from frank.daemon.registry import SessionRegistry

    if _acquire_singleton_lock() is None:
        return await _defer_to_running_daemon()
    _reclaim_socket()

    hub_state.global_configuration = Configuration.load()
    # Ask once, at boot, whether this machine can enforce a profile — and on macOS ask by running
    # one, because `sandbox-exec` being on disk and Apple still honouring it are different
    # questions, and the interface is deprecated. Sessions refuse individually when they must; this
    # is so the answer is in the log before the first one does.
    confinement_state = confinement.probe()
    if confinement_state["backend"]:
        logger.info("confinement backend: %s", confinement_state["detail"])
    else:
        logger.warning(
            "no confinement backend (%s). Sessions will refuse to start unless sandbox.enforce "
            "is set to 'preferred' or 'off'.", confinement_state["detail"],
        )
    state.daemon_token = secrets.token_urlsafe(32)
    state.daemon_socket = str(daemon_socket_path())
    hub_state.daemon_port = _free_port()

    await _open_stores()

    # Before any session exists. A background shell subtree survives a SIGKILL of the harness —
    # a dev server holding a port is the usual case — and each job recorded its process group
    # when it started, so this is the one moment those can be cleaned up without racing a
    # session that legitimately owns one.
    orphans = await asyncio.to_thread(reap_orphaned_process_groups)
    if orphans:
        logger.info("reaped %d orphaned process group(s) from a previous run", orphans)

    # The registry is durable now: a daemon restart ends every session's *process*, not every
    # session. Live records come back asleep, and the first message to one forks it a worker.
    hub_state.session_store = SqliteSessionStore(hub_state.session_factory)
    state.registry = SessionRegistry(store=hub_state.session_store)
    restored = await asyncio.to_thread(hub_state.session_store.load_all)
    state.registry.restore(restored)
    live = [record for record in restored if record.is_live]
    if live:
        logger.info("restored %d session(s), %d of them still live and asleep", len(restored), len(live))
    # The prototype and the lifecycle know about each other in both directions: the lifecycle
    # asks the prototype to fork, and the prototype reports every death back to the lifecycle.
    # Wired here rather than by either of them, because a composition root is exactly the place
    # a cycle between two collaborators becomes two one-way dependencies.
    state.prototype = PrototypeClient(
        on_exit=lambda report: state.lifecycle.on_session_exit(report),
        on_lost=lambda: state.lifecycle.on_prototype_lost(),
    )
    state.lifecycle = SessionLifecycle(
        state.registry,
        state.prototype,
        on_change=lambda: state.broadcaster.publish({"type": "sessions_changed"}),
    )
    # The two places a workspace change has a supervision consequence. Filled in here because
    # a composition root is exactly what decides whether there *is* a control plane to tell —
    # `frank web` serving the same workspace without one leaves them unset, and the workspace
    # goes on working.
    from frank.daemon.pending_input import settle_and_reap

    hub_state.on_session_deleted = settle_and_reap
    hub_state.reset_live_session_runtimes = state.reset_live_session_runtimes
    hub_state.refresh_live_session_locations = state.refresh_workspace_locations

    # Best effort: a machine that cannot start the prototype still serves the browser surface
    # and every read, and says so in `daemon.status`, rather than refusing to boot.
    try:
        await state.prototype.start()
    except Exception:  # noqa: BLE001 — a daemon without a prototype is degraded, not dead
        logger.error("the prototype could not be started, new sessions will fail", exc_info=True)
    # Built after the stores and the prototype, because the shared resources read from both, and
    # after the port is known, because the file-URL signer signs against this daemon's address.
    await open_shared_resources()

    app = build_app()
    announcing = _announcing_server_class()
    # `log_config=None` leaves uvicorn's loggers alone so they inherit the root configuration
    # set in `main()` — stderr *and* `frankd.log`. Uvicorn's default config does the opposite:
    # it binds `uvicorn.error` to a stderr handler with `propagate=False`, so an unhandled
    # exception in a route is written to a stream nobody is keeping and never reaches the file.
    # A `GET /sessions` that raised `AttributeError` on every call therefore answered 500 in
    # complete silence, through a merge and a packaged build, until someone thought to curl it.
    socket_server = announcing(
        uvicorn.Config(
            app, uds=state.daemon_socket, log_level="warning", access_log=False, log_config=None,
            # Only the unix listener: the kernel can name the process on the other end of a
            # local socket, and that is what identifies a session. The loopback listener is
            # the desktop client and has no such identity to offer.
            http=unix_peer_protocol(),
        )
    )
    tcp_server = announcing(
        uvicorn.Config(
            app, host=LOOPBACK_HOST, port=hub_state.daemon_port,
            log_level="warning", access_log=False, log_config=None,
        )
    )
    # uvicorn captures SIGTERM/SIGINT itself, and with two servers sharing a process each
    # would install a handler that stops only itself — so a signal would down one listener and
    # leave the other running, which is exactly how `stop` came back as "still running".
    # Neutralise both and let the handler below stop them together.
    for server in (socket_server, tcp_server):
        server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]

    stopping = asyncio.Event()

    def _stop() -> None:
        stopping.set()

    loop = asyncio.get_running_loop()
    for received in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(received, _stop)

    async def _shutdown_on_signal() -> None:
        """Wait for a signal, then tell both servers to stop.

        Setting `should_exit` from inside the loop rather than from the signal handler is what
        makes it take effect: uvicorn only observes the flag between its own iterations, so a
        handler that sets it while the loop is parked leaves the daemon running until something
        else happens to wake it — which is how a `stop` came back as "still running"."""
        await stopping.wait()
        # Streams first, servers second. An open SSE response — someone left `frank attach`
        # running in another terminal — holds the connection until it yields its last frame,
        # and uvicorn will not finish draining until it does. Closing the buses here is what
        # keeps `daemon stop` from waiting on a watcher that is itself waiting on the stop.
        state.event_bus.complete_all()
        state.broadcaster.close()
        socket_server.should_exit = True
        tcp_server.should_exit = True

    watcher = asyncio.create_task(_shutdown_on_signal())
    serving = asyncio.gather(socket_server.serve(), tcp_server.serve())
    # Wait for both listeners to be up, or for serving to fail — whichever happens first, so a
    # daemon that cannot bind reports that instead of waiting on a readiness that never comes.
    both_ready = asyncio.gather(socket_server.ready.wait(), tcp_server.ready.wait())
    await asyncio.wait({both_ready, serving}, return_when=asyncio.FIRST_COMPLETED)
    if serving.done():
        both_ready.cancel()
        await serving
        return 1
    _write_handshake(state.daemon_token, hub_state.daemon_port)
    # One line on stdout, then close it: whoever started the daemon is waiting to read exactly
    # this, and leaving the pipe open would let later output block on a reader that has gone.
    with contextlib.suppress(OSError, ValueError):
        sys.stdout.write(json.dumps({"ready": True, "pid": os.getpid(), "port": hub_state.daemon_port}) + "\n")
        sys.stdout.flush()
        sys.stdout.close()
    logger.info("frankd listening on %s and %s:%d", state.daemon_socket, LOOPBACK_HOST, hub_state.daemon_port)

    try:
        await serving
    finally:
        watcher.cancel()
        # Sessions must not outlive their supervisor: a session whose daemon is gone can no
        # longer persist anything, so leaving it running would silently lose work.
        with contextlib.suppress(Exception):
            await state.lifecycle.aclose()
        with contextlib.suppress(Exception):
            await state.prototype.aclose()
        with contextlib.suppress(Exception):
            await close_shared_resources()
        _clear_handshake()
    return 0


async def _open_stores() -> None:
    """Open the databases the daemon alone writes."""
    from sqlalchemy import create_engine, event
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.orm import sessionmaker

    from frank.base.paths import database_file_path
    from frank.base.sqlite_lock import configure_sqlite_lock, sqlite_write_lock
    from frank.hub import state as hub_state
    from frank.hub.database import _apply_history_schema
    from frank.daemon.persistence.turn_store import AppendOnlyTaskStore

    database_path = database_file_path()
    configure_sqlite_lock(database_path)
    sync_engine = create_engine(f"sqlite:///{database_path}")

    @event.listens_for(sync_engine, "connect")
    def _pragmas(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    def _initialize() -> None:
        with sqlite_write_lock():
            _apply_history_schema(sync_engine)

    await asyncio.to_thread(_initialize)
    hub_state.session_factory = sessionmaker(bind=sync_engine)
    hub_state.async_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", connect_args={"timeout": 30})

    @event.listens_for(hub_state.async_engine.sync_engine, "connect")
    def _async_pragmas(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    hub_state.turn_store = AppendOnlyTaskStore(hub_state.async_engine)
    await hub_state.turn_store.initialize()
    # A turn that was mid-execution when the daemon last stopped cannot be resurrected — its
    # worker is gone — so it is marked interrupted rather than left claiming to be running.
    interrupted = await hub_state.turn_store.reconcile_orphaned_turns()
    if interrupted:
        logger.warning("marked %d interrupted turn(s) from a previous run", len(interrupted))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(log_file_path("frankd"))],
    )
    try:
        return asyncio.run(_serve())
    except KeyboardInterrupt:
        return 0
    except SystemExit as exit_request:
        logger.error("%s", exit_request)
        return 1


if __name__ == "__main__":
    sys.exit(main())
