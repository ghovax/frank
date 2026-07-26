"""`daisyd`: the control plane.

It owns the registry, the worker pool, the databases, and the shared brokers, and it serves
one API two ways — a unix socket for the CLI and for sessions, and a loopback TCP port for
the desktop client, which cannot open a socket. Both require the capability token written
beside them in the runtime directory.

The daemon runs no agents. Everything that executes a turn lives in a worker process, which
is what keeps this side light enough to pre-fork workers from.
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
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from daisy.base.paths import (
    daemon_port_path,
    daemon_socket_path,
    daemon_token_path,
    log_file_path,
    runtime_directory,
)

logger = logging.getLogger("daisy.daemon")

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
    # The pid is how `daisy daemon stop` reaches a daemon that has stopped answering.
    pidfile = daemon_socket_path().parent / "daisyd.pid"
    pidfile.write_text(str(os.getpid()))
    pidfile.chmod(0o600)


def _clear_handshake() -> None:
    for path in (
        daemon_token_path(),
        daemon_port_path(),
        daemon_socket_path(),
        daemon_socket_path().parent / "daisyd.pid",
    ):
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def _acquire_singleton_lock() -> int | None:
    """An exclusive, process-lifetime lock: exactly one daemon per user.

    The socket alone cannot enforce this. Two `daisy` commands run at the same moment both find
    no daemon, both start one, and uvicorn unlinks an existing unix socket before binding — so
    the second daemon silently takes the socket from the first and the first becomes an orphan
    holding a pool of workers nothing will ever reap. An advisory lock is decided by the kernel
    rather than by who checked first, which is what closes the window between looking and
    binding.

    The descriptor is deliberately never closed: the lock lives exactly as long as the process
    that holds it, and the kernel drops it even if the daemon is killed outright."""
    path = runtime_directory() / "daisyd.lock"
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
    socket itself becoming answerable."""
    deadline = time.monotonic() + 30.0
    path = daemon_socket_path()
    while time.monotonic() < deadline:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(str(path))
        except OSError:
            await asyncio.sleep(0.05)
            continue
        finally:
            probe.close()
        with contextlib.suppress(OSError, ValueError):
            sys.stdout.write(json.dumps({"ready": True, "deferred": True}) + "\n")
            sys.stdout.flush()
            sys.stdout.close()
        logger.info("Another daisyd already holds the runtime directory; standing down.")
        return 0
    logger.error("Another daisyd holds the lock but never started serving.")
    return 1


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
    from daisy.daemon import state
    from daisy.daemon.api import RpcError
    from daisy.daemon.api import router as control_router
    from daisy.daemon.ingest import router as ingest_router
    from daisy.daemon.peer_identity import calling_session
    from daisy.rest.app import mount as mount_gui_routes

    app = FastAPI(title="daisyd")
    # The desktop client's webview is a browser, and a browser will not send a request its
    # origin policy forbids. Scoped to the app's own origins — reflecting `*` would let any
    # page the user happened to visit script a local, tool-executing API.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_APP_ORIGIN_PATTERN,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    @app.exception_handler(RpcError)
    async def _rpc_error(_request: Request, error: RpcError) -> JSONResponse:
        """The same error shape whether it was raised under `/rpc` or under a plain route.

        `/rpc` catches these itself; the streaming routes do not, and without this an
        `attach` to a session that does not exist came back as an opaque 500 with the
        reason — the one useful part — swallowed by the default handler."""
        return JSONResponse(
            {"error": {"code": error.code, "message": error.message}}, status_code=error.status_code
        )

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        """Every call carries the token. Discovery of the daemon's existence is not a secret;
        driving it is."""
        if request.url.path in {"/health"}:
            return await call_next(request)
        header = request.headers.get("Authorization", "")
        # A WebSocket handshake and an <img>/<iframe> URL cannot carry a header, so the
        # terminal and artifact surfaces pass the same token as a query parameter.
        presented = (
            header[len("Bearer "):] if header.startswith("Bearer ")
            else request.query_params.get("token", "")
        )
        # Who the *kernel* says is calling, which no token can contradict. A session's own
        # shell can read the daemon's 0600 token file — same user, same machine — so a token
        # alone could never establish that a caller was not a session. This can.
        peer_session = calling_session(request.scope)

        if presented and secrets.compare_digest(presented, state.daemon_token):
            # The daemon token says "you may drive this daemon" and nothing about who is
            # asking — so a caller the kernel identified as a session is still that session,
            # holding this token or not. That is what closes the escape hatch: reading the
            # token buys a session no anonymity.
            request.state.calling_session = peer_session or ""
            return await call_next(request)
        # A session's own token identifies *which* session is calling, which the daemon token
        # cannot. That attribution is what lets a session's control-plane calls be attributed
        # to it — so a peer it creates is its child, and is clamped against it, whatever the
        # request body claims.
        caller = state.registry.session_for_token(presented) if (presented and state.registry) else None
        if caller is None:
            return JSONResponse({"error": {"code": "unauthorized", "message": "Bad or missing token."}}, status_code=401)
        # The kernel's answer wins over the token's when both are available: a session holding
        # another session's token is still itself.
        request.state.calling_session = peer_session or caller.id
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict:
        """Unauthenticated on purpose: a client needs to know the daemon is up before it has
        read the token, and this says nothing else."""
        return {"ok": True, "service": "daisyd"}

    app.include_router(control_router)
    app.include_router(ingest_router)
    mount_gui_routes(app)
    return app


async def _serve() -> int:
    import uvicorn

    from daisy.base import confinement
    from daisy.base.configuration import GlobalConfiguration
    from daisy.daemon import state
    from daisy.daemon.composition import close_shared_resources, open_shared_resources
    from daisy.daemon.lifecycle import SessionLifecycle
    from daisy.daemon.peer_identity import unix_peer_protocol
    from daisy.daemon.pool import WorkerPool
    from daisy.daemon.registry import SessionRegistry

    if _acquire_singleton_lock() is None:
        return await _defer_to_running_daemon()
    _reclaim_socket()

    state.global_configuration = GlobalConfiguration.load()
    # Ask once, at boot, whether this machine can enforce a profile — and on macOS ask by running
    # one, because `sandbox-exec` being on disk and Apple still honouring it are different
    # questions, and the interface is deprecated. Sessions refuse individually when they must; this
    # is so the answer is in the log before the first one does.
    confinement_state = confinement.probe()
    if confinement_state["backend"]:
        logger.info("Confinement backend: %s", confinement_state["detail"])
    else:
        logger.warning(
            "No confinement backend (%s). Sessions will refuse to start unless sandbox.enforce "
            "is set to 'preferred' or 'off'.", confinement_state["detail"],
        )
    state.daemon_token = secrets.token_urlsafe(32)
    state.daemon_socket = str(daemon_socket_path())
    state.daemon_port = _free_port()

    await _open_stores()

    state.registry = SessionRegistry()
    state.pool = WorkerPool(
        floor=state.global_configuration.daemon.warm_floor,
        ceiling=state.global_configuration.daemon.warm_ceiling,
    )
    state.lifecycle = SessionLifecycle(
        state.registry,
        state.pool,
        on_change=lambda: state.broadcaster.publish({"type": "sessions_changed"}),
    )
    await state.pool.start()
    # Built after the stores and the pool, because the shared resources read from both, and
    # after the port is known, because the file-URL signer signs against this daemon's address.
    await open_shared_resources()

    app = build_app()
    announcing = _announcing_server_class()
    socket_server = announcing(
        uvicorn.Config(
            app, uds=state.daemon_socket, log_level="warning", access_log=False,
            # Only the unix listener: the kernel can name the process on the other end of a
            # local socket, and that is what identifies a session. The loopback listener is
            # the desktop client and has no such identity to offer.
            http=unix_peer_protocol(),
        )
    )
    tcp_server = announcing(
        uvicorn.Config(app, host=LOOPBACK_HOST, port=state.daemon_port, log_level="warning", access_log=False)
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
        # Streams first, servers second. An open SSE response — someone left `daisy attach`
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
    _write_handshake(state.daemon_token, state.daemon_port)
    # One line on stdout, then close it: whoever started the daemon is waiting to read exactly
    # this, and leaving the pipe open would let later output block on a reader that has gone.
    with contextlib.suppress(OSError, ValueError):
        sys.stdout.write(json.dumps({"ready": True, "pid": os.getpid(), "port": state.daemon_port}) + "\n")
        sys.stdout.flush()
        sys.stdout.close()
    logger.info("daisyd listening on %s and %s:%d", state.daemon_socket, LOOPBACK_HOST, state.daemon_port)

    try:
        await serving
    finally:
        watcher.cancel()
        # Sessions must not outlive their supervisor: a worker whose daemon is gone can no
        # longer persist anything, so leaving it running would silently lose work.
        with contextlib.suppress(Exception):
            await state.lifecycle.aclose()
        with contextlib.suppress(Exception):
            await state.pool.aclose()
        with contextlib.suppress(Exception):
            await close_shared_resources()
        _clear_handshake()
    return 0


async def _open_stores() -> None:
    """Open the databases the daemon alone writes."""
    from sqlalchemy import create_engine, event
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.orm import sessionmaker

    from daisy.base.paths import database_file_path
    from daisy.base.sqlite_lock import configure_sqlite_lock, sqlite_write_lock
    from daisy.daemon import state
    from daisy.daemon.persistence.database import _apply_history_schema
    from daisy.daemon.persistence.turn_store import AppendOnlyTaskStore

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
    state.session_factory = sessionmaker(bind=sync_engine)
    state.async_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", connect_args={"timeout": 30})

    @event.listens_for(state.async_engine.sync_engine, "connect")
    def _async_pragmas(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    state.turn_store = AppendOnlyTaskStore(state.async_engine)
    await state.turn_store.initialize()
    # A turn that was mid-execution when the daemon last stopped cannot be resurrected — its
    # worker is gone — so it is marked interrupted rather than left claiming to be running.
    interrupted = await state.turn_store.reconcile_orphaned_turns()
    if interrupted:
        logger.warning("Marked %d interrupted turn(s) from a previous run.", len(interrupted))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(log_file_path("daisyd"))],
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
