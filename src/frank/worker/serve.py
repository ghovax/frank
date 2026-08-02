"""Becoming a session: take an assignment, bind a socket, serve until told to stop.

This is everything a worker process does after it knows what it is. It was the second half
of the worker's entry point, when a worker was a process the daemon started and then told
over stdin; a worker is now a fork of the prototype, which already has the assignment in
memory, so there is nothing to read and no entry point to be.

Readiness is reported on a file descriptor rather than on stdout for the same reason. A
forked child shares its parent's stdout, and the parent is the prototype, whose stdout is
the daemon's log — a readiness line written there would be a log entry nobody reads instead
of an answer somebody is waiting for. The descriptor is a pipe the prototype created for
this one fork and is watching.

The acknowledgement is sent only once the socket is accepting connections, which is the
property the whole thing exists for: a client that sends immediately after `create` must
not race the bind.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import signal
import sys
from pathlib import Path

from frank.base.serialization import compact

logger = logging.getLogger("frank.worker")


def _report(ready_fd: int, payload: dict) -> None:
    """Answer the prototype on the pipe it is watching, once.

    Failures are swallowed deliberately: the prototype may already have given up and closed
    its end, and a session that is otherwise healthy must not die because nobody was
    listening for its acknowledgement."""
    if ready_fd < 0:
        return
    with contextlib.suppress(OSError):
        os.write(ready_fd, (compact(payload) + "\n").encode())
    with contextlib.suppress(OSError):
        os.close(ready_fd)


async def serve(assignment: dict, ready_fd: int = -1, lifeline_fd: int = -1) -> int:
    """Run one session until its socket closes. Answers with the process exit status."""
    from frank.base.configuration import Configuration
    from frank.worker.server import build_app
    from frank.worker.session import SessionExecutor

    session_id = str(assignment.get("session_id") or "")
    socket_path = Path(str(assignment.get("socket") or ""))
    agent_name = str(assignment.get("agent") or "")
    if not session_id or not str(socket_path):
        _report(ready_fd, {"ready": False, "reason": "assignment is missing a session id or socket path"})
        return 1
    if not agent_name:
        # There is no default to fall back to, and running an unnamed profile would mean a
        # session whose behaviour nobody chose. Refusing is the only honest answer.
        _report(ready_fd, {"ready": False, "reason": "assignment is missing the agent to run"})
        return 1

    # Every subprocess this session starts inherits its identity, so a session that reaches
    # for the `frank` command from a shell parents its peers correctly instead of orphaning
    # them. The tools are the path meant to be taken and carry this themselves; this is what
    # keeps the other path from being silently wrong.
    os.environ["FRANK_SESSION_ID"] = session_id

    configuration = Configuration.load()
    session = SessionExecutor(
        session_id=session_id,
        agent_name=agent_name,
        working_directory=str(assignment.get("working_directory") or ""),
        runtime_working_directory=str(assignment.get("runtime_working_directory") or ""),
        permission_mode=str(assignment.get("permission_mode") or "default"),
        sandbox=assignment.get("sandbox") or {},
        workspace_id=str(assignment.get("workspace_id") or ""),
        locations=assignment.get("locations") or None,
        parent=str(assignment.get("parent") or ""),
        token=str(assignment.get("token") or ""),
        daemon_token=str(assignment.get("daemon_token") or ""),
        global_configuration=configuration,
    )
    await session.start()

    # Claim the session before touching its socket, and hold the claim for as long as this
    # process lives.
    #
    # A session is one conversation, and one conversation cannot have two writers. The daemon
    # tries to arrange that — a wake lock, a sleeping check, a reachability check — but every one
    # of those is an *inference* about whether a worker exists, and an inference that is wrong
    # once forks a second worker. Which used to be enough, because the line below unlinks the
    # socket path unconditionally: the newcomer took the path from the incumbent, both served the
    # same session id, each with its own runtime and its own copy of the conversation, and a
    # single question came back answered twice.
    #
    # A lock makes the arrangement a fact instead of an intention. The kernel releases it when
    # the holder dies, however it dies, so the stale-socket case the unlink was written for still
    # works: no live holder means the lock is free, and the newcomer is the rightful owner.
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    claim = os.open(str(socket_path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(claim, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(claim)
        _report(ready_fd, {"ready": False, "reason": "this session already has a worker"})
        await session.aclose()
        return 1
    # Now that nothing else can be serving it, a socket still on disk is a dead process's leavings.
    with contextlib.suppress(OSError):
        socket_path.unlink(missing_ok=True)

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

    # The lifeline, watched for as long as this session runs rather than checked once. It only
    # ever becomes readable by reaching end-of-file, because nothing is ever written to it: the
    # prototype holds the far end open and closes it by dying.
    #
    # This is what a signal cannot do. Ending a session by signalling it needs the prototype to
    # still be alive enough to send one, so a prototype that was killed outright, or that
    # crashed, left every session it had forked running — reparented, still holding its socket,
    # and unreapable, because the only process that could ever have reported their deaths was
    # the one that had just died. Stopping here goes through the same path as a `SIGTERM`, so
    # the turn is abandoned and the conversation checkpointed on the way out rather than lost.
    def _prototype_is_gone() -> None:
        with contextlib.suppress(OSError, ValueError):
            loop.remove_reader(lifeline_fd)
        logger.warning("the prototype is gone; stopping this session")
        stopping.set()

    if lifeline_fd >= 0:
        with contextlib.suppress(OSError, ValueError, NotImplementedError):
            os.set_blocking(lifeline_fd, False)
            loop.add_reader(lifeline_fd, _prototype_is_gone)

    shutdown_watcher = asyncio.create_task(_shutdown_on_signal())

    serve_task = asyncio.create_task(server.serve())
    # Waiting on the readiness event and on the serve task together means a worker that
    # cannot bind is reported as failed instead of hanging its creator until the assignment
    # times out.
    ready_wait = asyncio.create_task(server.ready.wait())
    await asyncio.wait({ready_wait, serve_task}, return_when=asyncio.FIRST_COMPLETED)
    if serve_task.done():
        ready_wait.cancel()
        _report(ready_fd, {"ready": False, "reason": "the session could not bind its socket"})
        await session.aclose()
        return 1
    _report(ready_fd, {"ready": True, "pid": os.getpid()})

    try:
        await serve_task
    finally:
        shutdown_watcher.cancel()
        if lifeline_fd >= 0:
            with contextlib.suppress(OSError, ValueError):
                loop.remove_reader(lifeline_fd)
            with contextlib.suppress(OSError):
                os.close(lifeline_fd)
        await session.aclose()
        with contextlib.suppress(OSError):
            socket_path.unlink(missing_ok=True)
        # The kernel would drop this when the process exits anyway; closing it here means an
        # orderly shutdown hands the session on without waiting for that.
        with contextlib.suppress(OSError):
            os.close(claim)
    return 0


def run(assignment: dict, ready_fd: int = -1, lifeline_fd: int = -1) -> int:
    """`serve` with its own event loop, for a process that has just been forked into being."""
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        return asyncio.run(serve(assignment, ready_fd, lifeline_fd))
    except KeyboardInterrupt:
        return 0
    except Exception:  # noqa: BLE001 — a child must report its failure, not vanish silently
        logger.exception("session process failed")
        _report(ready_fd, {"ready": False, "reason": "the session process raised on startup"})
        return 1


__all__ = ["run", "serve"]


if __name__ == "__main__":  # pragma: no cover — not an entry point, kept out of argv routing
    sys.exit(1)
