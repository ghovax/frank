"""The worker process: park blank, take an assignment, become that session for life.

A worker starts before anyone knows which agent it will run. It imports the runtime — the
expensive part, and the same for every session — then blocks on stdin waiting to be told what
it is. That is what makes spawning a session feel like a socket write rather than a Python
cold start.

Once assigned it is that session until it dies. It never goes back to the pool and never
serves a second session, so there is no path by which one session's state could reach
another's; isolation is a property of the process, not of any cleanup code.

Deliberately: the accessibility surfaces are imported lazily, inside the tools that use them,
so a parked worker has never loaded PyObjC. A blank worker is therefore safe to fork.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from pathlib import Path
from daisy.base.serialization import compact

logger = logging.getLogger("daisy.worker")


async def _read_assignment() -> dict | None:
    """Block until the daemon says what this worker is.

    Reading stdin off the event loop keeps the process responsive to signals while it waits,
    so a pool being torn down does not leave parked workers ignoring SIGTERM."""
    loop = asyncio.get_running_loop()
    line = await loop.run_in_executor(None, sys.stdin.readline)
    if not line:
        return None
    try:
        return json.loads(line)
    except ValueError:
        logger.error("Assignment was not valid JSON; exiting")
        return None


async def run() -> int:
    from daisy.base.configuration import GlobalConfiguration
    from daisy.worker.server import build_app
    from daisy.worker.session import SessionExecutor

    assignment = await _read_assignment()
    if assignment is None:
        return 0

    session_id = str(assignment.get("session_id") or "")
    socket_path = Path(str(assignment.get("socket") or ""))
    agent_name = str(assignment.get("agent") or "")
    if not session_id or not str(socket_path):
        logger.error("Assignment is missing a session id or socket path")
        return 1
    if not agent_name:
        # There is no default to fall back to, and running an unnamed profile would mean a
        # session whose behaviour nobody chose. Refusing is the only honest answer.
        logger.error("Assignment is missing the agent to run")
        return 1

    # Every subprocess this session starts inherits its identity, so a session that reaches
    # for the `daisy` command from a shell parents its peers correctly instead of orphaning
    # them. The tools are the path meant to be taken and carry this themselves; this is what
    # keeps the other path from being silently wrong.
    os.environ["DAISY_SESSION_ID"] = session_id

    configuration = GlobalConfiguration.load()
    session = SessionExecutor(
        session_id=session_id,
        agent_name=agent_name,
        working_directory=str(assignment.get("working_directory") or ""),
        runtime_working_directory=str(assignment.get("runtime_working_directory") or ""),
        permission_mode=str(assignment.get("permission_mode") or "default"),
        sandbox=assignment.get("sandbox") or {},
        project_id=str(assignment.get("project_id") or ""),
        parent=str(assignment.get("parent") or ""),
        token=str(assignment.get("token") or ""),
        daemon_token=str(assignment.get("daemon_token") or ""),
        global_configuration=configuration,
    )
    await session.start()

    # A socket left behind by a previous process would make bind fail; the daemon unlinks on
    # reap, but a hard kill leaves one, so clear the path before claiming it.
    with contextlib.suppress(OSError):
        socket_path.unlink(missing_ok=True)
    socket_path.parent.mkdir(parents=True, exist_ok=True)

    import uvicorn

    class _AnnouncingServer(uvicorn.Server):
        """Sets an event once the socket is accepting connections.

        uvicorn only exposes readiness as an attribute to be polled; making it awaitable is
        what lets the worker report ready at the exact moment it can serve, rather than a
        sleep-interval later."""

        def __init__(self, configuration) -> None:  # noqa: ANN001 — matches uvicorn's signature
            super().__init__(configuration)
            self.ready = asyncio.Event()

        async def startup(self, sockets=None) -> None:  # noqa: ANN001
            await super().startup(sockets=sockets)
            self.ready.set()

    config = uvicorn.Config(
        build_app(session),
        uds=str(socket_path),
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    server = _AnnouncingServer(config)

    # uvicorn captures the termination signals itself; the worker wants its own teardown to
    # run first, so the session's turn is aborted and its conversation checkpointed before the
    # socket goes away.
    server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]
    stopping = asyncio.Event()

    async def _shutdown_on_signal() -> None:
        # Set from inside the loop rather than from the handler: uvicorn only observes the
        # flag between its own iterations.
        await stopping.wait()
        server.should_exit = True

    loop = asyncio.get_running_loop()
    for received in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(received, stopping.set)
    shutdown_watcher = asyncio.create_task(_shutdown_on_signal())

    serve = asyncio.create_task(server.serve())
    # Only once the socket is accepting connections is the session usable, so readiness is
    # reported here rather than at assignment: a client that sends immediately after `create`
    # must not race the bind. Waiting on the readiness event and on the serve task together
    # means a worker that cannot bind is reported as failed instead of hanging its creator
    # until the assignment times out.
    ready_wait = asyncio.create_task(server.ready.wait())
    await asyncio.wait({ready_wait, serve}, return_when=asyncio.FIRST_COMPLETED)
    if serve.done():
        ready_wait.cancel()
        await session.aclose()
        return 1
    sys.stdout.write(compact({"ready": True, "pid": os.getpid()}) + "\n")
    sys.stdout.flush()

    try:
        await serve
    finally:
        shutdown_watcher.cancel()
        await session.aclose()
        with contextlib.suppress(OSError):
            socket_path.unlink(missing_ok=True)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
