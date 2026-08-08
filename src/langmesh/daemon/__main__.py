"""`langmeshd`, the control plane: it owns the registry, the sessions and the stores, and serves one API over a socket and loopback."""

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
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_delay,
    wait_fixed,
)

from langmesh.base.paths import (
    daemon_port_path,
    daemon_socket_path,
    daemon_token_path,
    log_file_path,
    runtime_directory,
)
from langmesh.base.tuning import Tunable, active_tuning

logger = logging.getLogger("langmesh.daemon")

# Bound to loopback only: the token authorises a call, and the bind keeps the surface off the network.
LOOPBACK_HOST = "127.0.0.1"

# The only browsers with any business here: the desktop app's own webview and a local development server.
_APP_ORIGIN_PATTERN = "^(tauri://localhost|https?://tauri\\.localhost|https?://localhost(:\\d+)?|https?://127\\.0\\.0\\.1(:\\d+)?)$"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK_HOST, 0))
        return probe.getsockname()[1]


def _write_handshake(token: str, port: int) -> None:
    """Publish where the daemon is and what proves you may talk to it, both 0600 so file permissions are the access control."""
    token_path = daemon_token_path()
    token_path.write_text(token)
    token_path.chmod(0o600)
    port_path = daemon_port_path()
    port_path.write_text(str(port))
    port_path.chmod(0o600)
    # The pid is how `langmesh daemon stop` reaches a daemon that has stopped answering.
    pidfile = daemon_socket_path().parent / "langmeshd.pid"
    pidfile.write_text(str(os.getpid()))
    pidfile.chmod(0o600)


def _clear_handshake() -> None:
    for path in (
        daemon_token_path(),
        daemon_port_path(),
        daemon_socket_path(),
        daemon_socket_path().parent / "langmeshd.pid",
    ):
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def _acquire_singleton_lock() -> int | None:
    """An exclusive process-lifetime lock, because two daemons started at once would each unlink the other's socket."""
    path = runtime_directory() / "langmeshd.lock"
    handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(handle)
        return None
    return handle


async def _defer_to_running_daemon() -> int:
    """Stand down for the daemon that won the lock, once it is actually serving, since somebody is waiting on that line."""
    path = daemon_socket_path()
    tuning = active_tuning()

    def probe_once() -> None:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(tuning.duration(Tunable.daemon_probe_connect))
        try:
            probe.connect(str(path))
        finally:
            probe.close()

    try:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(OSError),
            wait=wait_fixed(tuning.duration(Tunable.daemon_probe_interval)),
            stop=stop_after_delay(tuning.duration(Tunable.daemon_startup)),
        ):
            with attempt:
                await asyncio.to_thread(probe_once)
    except RetryError:
        logger.error("another langmeshd holds the lock but never started serving")
        return 1
    with contextlib.suppress(OSError, ValueError):
        sys.stdout.write(json.dumps({"ready": True, "deferred": True}) + "\n")
        sys.stdout.flush()
        sys.stdout.close()
    logger.info("another langmeshd already holds the runtime directory, standing down")
    return 0


def _reclaim_socket() -> None:
    """Remove a socket left by a daemon that died, testing whether anything is actually listening rather than whether the file exists."""
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
        """A uvicorn server that sets an event once it is accepting connections, so nothing has to poll for readiness."""

        def __init__(self, config) -> None:  # noqa: ANN001 — matches uvicorn's signature
            super().__init__(config)
            self.ready = asyncio.Event()

        async def startup(self, sockets=None) -> None:  # noqa: ANN001
            await super().startup(sockets=sockets)
            self.ready.set()

    return AnnouncingServer


def build_app() -> FastAPI:
    from langmesh.daemon import state
    from langmesh.daemon.api import RpcError
    from langmesh.daemon.api import router as control_router
    from langmesh.daemon.ingest import router as ingest_router
    from langmesh.daemon.peer_identity import calling_session
    from langmesh.rest.app import mount as mount_gui_routes

    app = FastAPI(title="langmeshd")

    @app.exception_handler(RpcError)
    async def _rpc_error(_request: Request, error: RpcError) -> JSONResponse:
        """The same error shape whether it was raised under `/rpc` or a plain route, so a stream failure still says why."""
        return JSONResponse(
            {"error": {"code": error.code, "message": error.message}}, status_code=error.status_code
        )

    # Pure ASGI rather than a decorated middleware, whose response pumping would break this daemon's long-lived streams.
    class Authenticate:
        def __init__(self, application):
            self.application = application

        async def __call__(self, scope, receive, send):
            # Websockets are checked here too, since a handshake reaches the same routes a request does.
            if scope["type"] not in {"http", "websocket"}:
                return await self.application(scope, receive, send)

            # `HTTPConnection` rather than `Request`, since a websocket scope is not a request and the fields read here are common to both.
            connection = HTTPConnection(scope)

            if scope["type"] == "http":
                if connection.url.path in {"/health"}:
                    return await self.application(scope, receive, send)
                # A preflight is sent without credentials by specification, so demanding a token rejects the question rather than the request.
                if (
                    scope.get("method") == "OPTIONS"
                    and "access-control-request-method" in connection.headers
                ):
                    return await self.application(scope, receive, send)

            header = connection.headers.get("Authorization", "")
            # A websocket handshake and an image URL cannot carry a header, so the token may also arrive as a query parameter.
            presented = (
                header[len("Bearer ") :]
                if header.startswith("Bearer ")
                else connection.query_params.get("token", "")
            )
            # Who the kernel says is calling, which no token can contradict and which is what identifies a session.
            peer_session = calling_session(scope)
            scope.setdefault("state", {})

            if presented and secrets.compare_digest(presented, state.daemon_token):
                # The daemon token says you may drive this daemon and nothing about who is asking, so a session stays itself.
                scope["state"]["calling_session"] = peer_session or ""
                return await self.application(scope, receive, send)
            # A session's own token identifies which session is calling, which is what lets its control-plane calls be attributed to it.
            caller = (
                state.registry.session_for_token(presented)
                if (presented and state.registry)
                else None
            )
            if caller is None:
                return await self._refuse(scope, receive, send)
            # The kernel's answer wins over the token's, so a session holding another's token is still itself.
            scope["state"]["calling_session"] = peer_session or caller.id
            return await self.application(scope, receive, send)

        @staticmethod
        async def _refuse(scope, receive, send) -> None:
            """Say no in the shape the caller's transport understands, refusing a handshake rather than accepting and closing."""
            if scope["type"] == "websocket":
                # 1008 is "policy violation", which is the closest the protocol has to a 401.
                return await WebSocketClose(code=1008)(scope, receive, send)
            response = JSONResponse(
                {"error": {"code": "unauthorized", "message": "Bad or missing token."}},
                status_code=401,
            )
            return await response(scope, receive, send)

    app.add_middleware(Authenticate)
    # Added after `Authenticate` so it ends up outermost, because a browser's preflight must be answered before the token check.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_APP_ORIGIN_PATTERN,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict:
        """Unauthenticated on purpose: a client needs to know the daemon is up before it has read the token."""
        return {"ok": True, "service": "langmeshd"}

    app.include_router(control_router)
    app.include_router(ingest_router)
    mount_gui_routes(app)
    return app


async def _serve() -> int:
    import uvicorn

    from langmesh.base import confinement
    from langmesh.base.background_store import reap_orphaned_process_groups
    from langmesh.base.configuration import Configuration
    from langmesh.daemon import state
    from langmesh.commons import state as commons_state
    from langmesh.daemon.composition import close_shared_resources, open_shared_resources
    from langmesh.daemon.lifecycle import SessionLifecycle
    from langmesh.daemon.peer_identity import unix_peer_protocol
    from langmesh.daemon.host import SessionHost
    from langmesh.daemon.persistence.sessions import SqliteSessionStore
    from langmesh.daemon.registry import SessionRegistry

    if _acquire_singleton_lock() is None:
        return await _defer_to_running_daemon()
    _reclaim_socket()

    commons_state.global_configuration = Configuration.load()
    if commons_state.global_configuration.user_context.enabled:
        # Built here, in the background, so the first message of a conversation never waits on it.
        from langmesh.runtime.prompt.environment import warm_user_context

        warm_user_context(commons_state.global_configuration.user_context.refresh_hours)
    # Ask once at boot whether this machine can enforce a profile, on macOS by running one rather than by looking for the binary.
    confinement_state = confinement.probe()
    if confinement_state["backend"]:
        logger.info("confinement backend: %s", confinement_state["detail"])
    else:
        logger.warning(
            "no confinement backend (%s). Sessions will refuse to start unless sandbox.enforce is set to 'preferred' or 'off'.",
            confinement_state["detail"],
        )
    state.daemon_token = secrets.token_urlsafe(32)
    state.daemon_socket = str(daemon_socket_path())
    commons_state.daemon_port = _free_port()

    await _open_stores()

    # Before any session exists, since a background shell subtree survives a kill and recorded its process group.
    orphans = await asyncio.to_thread(reap_orphaned_process_groups)
    if orphans:
        logger.info("reaped %d orphaned process group(s) from a previous run", orphans)

    # The registry is durable, so a restart ends every session's process rather than every session.
    commons_state.session_store = SqliteSessionStore(commons_state.session_factory)
    state.registry = SessionRegistry(store=commons_state.session_store)
    restored = await asyncio.to_thread(commons_state.session_store.load_all)
    state.registry.restore(restored)
    live = [record for record in restored if record.is_live]
    if live:
        logger.info(
            "restored %d session(s), %d of them still live and asleep", len(restored), len(live)
        )
    # The host holds the executors and the lifecycle drives them, wired here because this is the composition root.
    state.host = SessionHost()
    # Imported at boot rather than when the first session is built, since that import is seconds and this is not a hot path.
    import langmesh.worker.session  # noqa: F401

    # How a call from a session's tool child is attributed back to that session.
    from langmesh.runtime import background as runtime_background

    runtime_background.note_child_group = state.host.note_child_group
    state.lifecycle = SessionLifecycle(
        state.registry,
        state.host,
        on_change=lambda: state.broadcaster.publish({"type": "sessions_changed"}),
    )
    # The two places a workspace change has a supervision consequence, filled in only where there is a control plane to tell.
    from langmesh.daemon.pending_input import settle_and_reap

    commons_state.on_session_deleted = settle_and_reap
    commons_state.reset_live_session_runtimes = state.reset_live_session_runtimes
    commons_state.refresh_live_session_locations = state.refresh_workspace_locations

    # Built after the stores, and after the port is known, because the file-URL signer signs against it.
    await open_shared_resources()

    app = build_app()
    announcing = _announcing_server_class()
    # `log_config=None` leaves uvicorn's loggers inheriting the root configuration, so its output reaches the log file too.
    socket_server = announcing(
        uvicorn.Config(
            app,
            uds=state.daemon_socket,
            log_level="warning",
            access_log=False,
            log_config=None,
            # Only the unix listener, because the kernel can name the process on the other end and that is what identifies a session.
            http=unix_peer_protocol(),
        )
    )
    tcp_server = announcing(
        uvicorn.Config(
            app,
            host=LOOPBACK_HOST,
            port=commons_state.daemon_port,
            log_level="warning",
            access_log=False,
            log_config=None,
        )
    )
    # uvicorn captures signals itself, so with two servers each handler would stop only its own listener.
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
        """Wait for a signal, then stop both servers from inside the loop, which is the only place `should_exit` is observed."""
        await stopping.wait()
        # Streams first, servers second, since an open response holds the connection until it yields its last frame.
        commons_state.shutting_down.set()
        state.event_bus.complete_all()
        state.broadcaster.close()
        socket_server.should_exit = True
        tcp_server.should_exit = True

    watcher = asyncio.create_task(_shutdown_on_signal())
    serving = asyncio.gather(socket_server.serve(), tcp_server.serve())
    # Wait for both listeners or for serving to fail, so a daemon that cannot bind reports it rather than hanging.
    both_ready = asyncio.gather(socket_server.ready.wait(), tcp_server.ready.wait())
    await asyncio.wait({both_ready, serving}, return_when=asyncio.FIRST_COMPLETED)
    if serving.done():
        both_ready.cancel()
        await serving
        return 1
    _write_handshake(state.daemon_token, commons_state.daemon_port)
    # One line on stdout and then close it, since whoever started the daemon is waiting to read exactly this.
    with contextlib.suppress(OSError, ValueError):
        sys.stdout.write(
            json.dumps({"ready": True, "pid": os.getpid(), "port": commons_state.daemon_port})
            + "\n"
        )
        sys.stdout.flush()
        sys.stdout.close()
    logger.info(
        "langmeshd listening on %s and %s:%d",
        state.daemon_socket,
        LOOPBACK_HOST,
        commons_state.daemon_port,
    )

    try:
        await serving
    finally:
        watcher.cancel()
        # Sessions must not outlive their supervisor, which can no longer persist anything for them.
        with contextlib.suppress(Exception):
            await state.lifecycle.aclose()
        with contextlib.suppress(Exception):
            await close_shared_resources()
        _clear_handshake()
    return 0


async def _open_stores() -> None:
    """Open the databases the daemon alone writes."""
    from sqlalchemy import create_engine, event
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.orm import sessionmaker

    from langmesh.base.paths import database_file_path
    from langmesh.base.sqlite_lock import configure_sqlite_lock, sqlite_write_lock
    from langmesh.commons import state as commons_state
    from langmesh.commons.database import create_history_schema
    from langmesh.daemon.persistence.turn_store import AppendOnlyTaskStore

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
            create_history_schema(sync_engine)

    await asyncio.to_thread(_initialize)
    commons_state.session_factory = sessionmaker(bind=sync_engine)
    commons_state.async_engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}", connect_args={"timeout": 30}
    )

    @event.listens_for(commons_state.async_engine.sync_engine, "connect")
    def _async_pragmas(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    commons_state.turn_store = AppendOnlyTaskStore(commons_state.async_engine)
    await commons_state.turn_store.initialize()
    # A turn mid-execution when the daemon stopped cannot be resurrected, so it is marked interrupted rather than left running.
    interrupted = await commons_state.turn_store.reconcile_orphaned_turns()
    if interrupted:
        logger.warning("marked %d interrupted turn(s) from a previous run", len(interrupted))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(log_file_path("langmeshd")),
        ],
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
