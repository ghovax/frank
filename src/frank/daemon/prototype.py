"""The daemon's end of the prototype: start it, keep it alive, ask it for sessions.

This is a client, not the prototype. The prototype itself lives in `worker/prototype.py`
because it must import the runtime, and the daemon must never — which is precisely why it is
a separate process rather than a function call. Everything here speaks to it over a unix
socket in newline-delimited JSON, and nothing here imports anything the prototype imports.

Three responsibilities, and they are all consequences of that separation:

**Supervision.** The daemon spawns the prototype and restarts it if it dies. A dead prototype
does not affect live sessions — they are independent processes, reparented to init, still
serving on their own sockets. Only *new* sessions block, and only for as long as the restart
takes.

**Forking.** `fork_session` is the replacement for claiming a warm worker. It writes the
assignment and waits for the child to report that its socket is accepting connections, which
is the same acknowledgement the old worker wrote to stdout and for the same reason: a client
that sends immediately after `create` must not race the bind.

**Exit reporting.** The daemon cannot `waitpid` a process it did not fork, so it cannot watch
a session die the way it used to. The prototype can, because it is the parent, and it reports
each exit here. `pidfd_open` on Linux and `kqueue` on macOS would also work; reporting needs
no platform-specific syscall and the prototype had to exist anyway.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from frank.base.fork_protocol import StartFailure, describe
from frank.base.paths import prototype_socket_path
from frank.base.tuning import Tunable, active_tuning

logger = logging.getLogger(__name__)


def prototype_command() -> list[str]:
    """How to launch the prototype.

    Deliberately a re-exec of *this* executable rather than a separate binary or a bare
    interpreter. In the packaged application the daemon runs as a signed helper inside the app
    bundle, and macOS attributes the Accessibility grant to that code identity; a prototype
    launched by any other path would be a different identity and would prompt the user for its
    own grant. Re-execing the same image is what keeps one grant covering the fleet — and
    because every session is a fork of this process, the grant reaches them too.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "prototype"]
    return [sys.executable, "-m", "frank", "prototype"]


@dataclass(frozen=True)
class SessionExit:
    """A session's process has ended, as observed by the process that forked it."""

    session_id: str
    pid: int
    code: int
    signal: int

    @property
    def clean(self) -> bool:
        return self.code == 0 and self.signal == 0

    def describe(self) -> str:
        if self.signal:
            return f"session process was killed by signal {self.signal}"
        return f"session process exited with status {self.code}"


class PrototypeUnavailable(RuntimeError):
    """The prototype is not running and could not be started, so no session can be forked."""


class PrototypeClient:
    """Owns the prototype process and the connection to it."""

    def __init__(
        self,
        *,
        environment: Optional[dict[str, str]] = None,
        on_exit: Optional[Callable[[SessionExit], Any]] = None,
        on_lost: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._environment = environment
        self._on_exit = on_exit
        # Called when the prototype dies unexpectedly, which is also the death of every session
        # it had forked — they hold lifelines to it. Nothing else can report those deaths, since
        # the process that forked them is the one that has gone.
        self._on_lost = on_lost
        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._pump: Optional[asyncio.Task] = None
        self._supervisor: Optional[asyncio.Task] = None
        # fork token -> the future waiting for *that* fork's readiness. Keyed by fork rather
        # than by session, because a session is forked afresh for every turn and its
        # incarnations must not be able to answer for one another.
        self._awaiting: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._status: dict[str, Any] = {}

    # Starting the prototype, and taking it down.

    async def start(self) -> None:
        """Bring the prototype up and stay attached to it."""
        await self._ensure_running()
        self._supervisor = asyncio.get_running_loop().create_task(self._supervise())

    async def aclose(self) -> None:
        self._closed = True
        for task in (self._supervisor, self._pump):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        await self._detach()
        await self._end_process()

    async def _end_process(self) -> None:
        """Stop the prototype and wait for it to go, killing it if it will not.

        Waiting matters rather than being tidy: the prototype's exit is what closes the
        lifelines its sessions hold, so returning before it has actually gone would be
        returning while the sessions are still running."""
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(
                process.wait(), timeout=active_tuning().duration(Tunable.sigterm_grace_seconds)
            )
        except (asyncio.TimeoutError, ProcessLookupError):
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
                await asyncio.wait_for(
                    process.wait(),
                    timeout=active_tuning().duration(Tunable.sigterm_grace_seconds),
                )

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def pid(self) -> int:
        return self._process.pid if self._process is not None else 0

    def status(self) -> dict[str, Any]:
        """What `daemon.status` reports about the prototype.

        `frozen` is not decoration: `gc.freeze()` being forgotten or reverted costs most of
        the memory saving and breaks nothing, so a zero here is the only way anyone would
        find out. `threads` is the invariant the whole design rests on."""
        return {
            "alive": self.alive,
            "pid": self.pid,
            "threads": int(self._status.get("threads") or 0),
            "frozen_objects": int(self._status.get("frozen") or 0),
            "sessions": int(self._status.get("children") or 0),
        }

    # The socket to the prototype, and the reports that come back over it.

    async def _ensure_running(self) -> None:
        async with self._lock:
            if self._closed:
                raise PrototypeUnavailable("the daemon is shutting down")
            if self.alive and self._writer is not None:
                return
            await self._detach()
            await self._spawn()
            await self._attach()

    async def _spawn(self) -> None:
        # A prototype that is still alive is ended before its replacement starts. Reaching here
        # while one is running means the connection to it broke without the process dying —
        # rare, but the descriptor being overwritten was the only thing holding it, so it and
        # every session under it would have been left with nothing that could ever reach them.
        await self._end_process()
        socket_path = prototype_socket_path()
        with contextlib.suppress(OSError):
            socket_path.unlink()
        try:
            self._process = await asyncio.create_subprocess_exec(
                *prototype_command(),
                env={**os.environ, **(self._environment or {})},
                # Its own process group, so signalling the daemon's group does not take the
                # prototype — and through it every session — down as collateral.
                start_new_session=True,
            )
        except OSError as error:
            raise PrototypeUnavailable(f"could not start the prototype: {error}") from error

    async def _attach(self) -> None:
        """Connect to the prototype's socket, waiting for it to appear.

        The prototype binds only after importing the runtime, which is the five seconds this
        whole design exists to pay once, so the wait is generous."""
        socket_path = prototype_socket_path()
        deadline = asyncio.get_running_loop().time() + active_tuning().duration(
            Tunable.prototype_start_seconds
        )
        last_error: Optional[BaseException] = None
        while asyncio.get_running_loop().time() < deadline:
            if self._process is not None and self._process.returncode is not None:
                raise PrototypeUnavailable(
                    f"the prototype exited during startup with status {self._process.returncode}"
                )
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(str(socket_path))
            except (OSError, asyncio.TimeoutError) as error:
                last_error = error
                await asyncio.sleep(0.05)
                continue
            self._pump = asyncio.get_running_loop().create_task(self._pump_reports())
            await self._request({"command": "status"})
            return
        raise PrototypeUnavailable(f"the prototype never accepted a connection: {last_error}")

    async def _detach(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump
            self._pump = None
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
                await self._writer.wait_closed()
        self._reader, self._writer = None, None

    async def _request(self, payload: dict[str, Any]) -> None:
        if self._writer is None:
            raise PrototypeUnavailable("not connected to the prototype")
        self._writer.write((json.dumps(payload) + "\n").encode())
        await self._writer.drain()

    async def _pump_reports(self) -> None:
        """Read the prototype's reports until the connection ends."""
        reader = self._reader
        if reader is None:
            return
        while True:
            line = await reader.readline()
            if not line:
                return
            try:
                message = json.loads(line.decode())
            except (ValueError, UnicodeDecodeError):
                logger.warning("ignoring a malformed report from the prototype")
                continue
            self._dispatch(message)

    def _dispatch(self, message: dict[str, Any]) -> None:
        event = str(message.get("event") or "")
        session_id = str(message.get("session_id") or "")
        if event == "status":
            self._status = message
            return
        # Every report about a child carries the token its fork was given, and that — never the
        # session id — is what finds the waiter. One session id covers every process the
        # conversation has ever had; a token covers exactly one.
        fork = str(message.get("fork") or "")
        if event in ("ready", "failed"):
            waiter = self._awaiting.pop(fork, None)
            if waiter is not None and not waiter.done():
                if event == "ready":
                    waiter.set_result(int(message.get("pid") or 0))
                else:
                    waiter.set_exception(PrototypeUnavailable(describe(
                        str(message.get("reason") or ""), str(message.get("detail") or ""),
                    )))
            return
        if event == "exited":
            # A session that never became ready exits too; settling its waiter here is what
            # stops `fork_session` waiting out its whole timeout for a process already gone.
            # Keyed by token, an exit can only ever settle the fork it actually belongs to —
            # a dead predecessor cannot fail the successor that replaced it.
            waiter = self._awaiting.pop(fork, None)
            if waiter is not None and not waiter.done():
                waiter.set_exception(
                    PrototypeUnavailable(describe(StartFailure.EXITED_BEFORE_SERVING))
                )
            if self._on_exit is not None:
                report = SessionExit(
                    session_id=session_id,
                    pid=int(message.get("pid") or 0),
                    code=int(message.get("code") if message.get("code") is not None else -1),
                    signal=int(message.get("signal") or 0),
                )
                result = self._on_exit(report)
                if asyncio.iscoroutine(result):
                    asyncio.get_running_loop().create_task(result)
            return
        logger.warning("ignoring an unknown report from the prototype: %s", event)

    # Keeping the prototype alive.

    async def _supervise(self) -> None:
        """Restart the prototype if it dies.

        Every session it had forked ends with it, through the lifeline each one holds. That is
        deliberate, and it replaces an arrangement where they carried on: a session outlives its
        prototype perfectly well as a *process* — it has its own socket and the daemon talks to
        it directly — but nothing can ever report its death again, because the only process that
        could was the one that just died. The daemon would show it running until the machine was
        rebooted, and a replacement prototype cannot adopt it: it is not its parent, so it cannot
        wait on it.

        So a dead prototype costs the sessions that were running, and they come back as records
        rather than as processes, which they were built to do. What it must not cost is a set of
        workers nobody can see or stop."""
        while not self._closed:
            process = self._process
            if process is None:
                return
            code = await process.wait()
            if self._closed:
                return
            logger.error("the prototype exited with status %s; restarting", code)
            await self._fail_everyone_waiting("the prototype died before the session started")
            # Before the restart, and before anything is forked into the replacement: the
            # sessions that died with it must be accounted for while it is still clear which
            # ones they were.
            if self._on_lost is not None:
                with contextlib.suppress(Exception):
                    result = self._on_lost()
                    if asyncio.iscoroutine(result):
                        await result
            try:
                await self._ensure_running()
            except PrototypeUnavailable as error:
                logger.error("could not restart the prototype: %s", error)
                await asyncio.sleep(active_tuning().duration(Tunable.prototype_restart_seconds))

    async def _fail_everyone_waiting(self, reason: str) -> None:
        waiting, self._awaiting = self._awaiting, {}
        for waiter in waiting.values():
            if not waiter.done():
                waiter.set_exception(PrototypeUnavailable(reason))

    # Asking for a session.

    async def fork_session(self, assignment: dict[str, Any]) -> int:
        """Fork a session and answer with its process id once it is serving.

        Raises :class:`PrototypeUnavailable` when the session could not be started, which the
        caller turns into a failed session record rather than a daemon-level error."""
        session_id = str(assignment.get("session_id") or "")
        if not session_id:
            raise PrototypeUnavailable("an assignment must name its session")
        await self._ensure_running()
        # This fork's own name. A session id names the *conversation*, and a conversation
        # outlives many processes — one per turn, since a session exits when its turn ends. So
        # a report keyed by session id says nothing about *which* incarnation it describes, and
        # the previous worker's `exited`, arriving after its replacement was forked, settled the
        # replacement's waiter and failed the wake. The token names one child, once.
        fork = uuid.uuid4().hex
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._awaiting[fork] = waiter
        try:
            await self._request({"command": "fork", "fork": fork, "assignment": assignment})
            return await asyncio.wait_for(
                waiter, timeout=active_tuning().duration(Tunable.session_start_seconds)
            )
        except asyncio.TimeoutError as error:
            raise PrototypeUnavailable(describe(StartFailure.NEVER_REPORTED)) from error
        finally:
            self._awaiting.pop(fork, None)

    async def refresh_status(self) -> dict[str, Any]:
        """Ask the prototype what it looks like right now, for `daemon.status`."""
        with contextlib.suppress(PrototypeUnavailable, OSError):
            await self._request({"command": "status"})
            # The report arrives on the pump; a short wait is enough for a local socket and
            # the previous snapshot is a fine answer if it is not.
            await asyncio.sleep(0.02)
        return self.status()


__all__ = ["PrototypeClient", "PrototypeUnavailable", "SessionExit", "prototype_command"]
