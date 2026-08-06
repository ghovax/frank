"""The daemon's end of the prototype: start it, keep it alive, ask it for sessions."""

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
    """How to launch the prototype."""
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
        # Called when the prototype dies unexpectedly, which is also the death of every session it had forked — they hold lifelines to it.
        self._on_lost = on_lost
        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._pump: Optional[asyncio.Task] = None
        self._supervisor: Optional[asyncio.Task] = None
        # fork token -> the future waiting for *that* fork's readiness.
        self._awaiting: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._status: dict[str, Any] = {}
        # Set by the pump when a status report lands, so `refresh_status` can wait for the answer instead of for a guess at how long the answer takes.
        self._status_arrived = asyncio.Event()

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
        """Stop the prototype and wait for it to go, killing it if it will not."""
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
        """What `daemon.status` reports about the prototype."""
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
        # A prototype that is still alive is ended before its replacement starts.
        await self._end_process()
        socket_path = prototype_socket_path()
        with contextlib.suppress(OSError):
            socket_path.unlink()
        try:
            self._process = await asyncio.create_subprocess_exec(
                *prototype_command(),
                env={**os.environ, **(self._environment or {})},
                # Its own process group, so signalling the daemon's group does not take the prototype — and through it every session — down as collateral.
                start_new_session=True,
            )
        except OSError as error:
            raise PrototypeUnavailable(f"could not start the prototype: {error}") from error

    async def _attach(self) -> None:
        """Connect to the prototype's socket, waiting for it to appear."""
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
            self._status_arrived.set()
            return
        # Every report about a child carries the token its fork was given, and that — never the session id — is what finds the waiter.
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
            # A session that never became ready exits too; settling its waiter here is what stops `fork_session` waiting out its whole timeout for a process already gone.
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
        """Restart the prototype if it dies."""
        while not self._closed:
            process = self._process
            if process is None:
                return
            code = await process.wait()
            if self._closed:
                return
            logger.error("the prototype exited with status %s; restarting", code)
            await self._fail_everyone_waiting("the prototype died before the session started")
            # Before the restart, and before anything is forked into the replacement: the sessions that died with it must be accounted for while it is still clear which ones they were.
            if self._on_lost is not None:
                with contextlib.suppress(Exception):
                    result = self._on_lost()
                    if asyncio.iscoroutine(result):
                        await result
            try:
                await self._ensure_running()
            except PrototypeUnavailable:
                logger.error("could not restart the prototype", exc_info=True)
                await asyncio.sleep(active_tuning().duration(Tunable.prototype_restart_seconds))

    async def _fail_everyone_waiting(self, reason: str) -> None:
        waiting, self._awaiting = self._awaiting, {}
        for waiter in waiting.values():
            if not waiter.done():
                waiter.set_exception(PrototypeUnavailable(reason))

    # Asking for a session.

    async def fork_session(self, assignment: dict[str, Any]) -> int:
        """Fork a session and answer with its process id once it is serving."""
        session_id = str(assignment.get("session_id") or "")
        if not session_id:
            raise PrototypeUnavailable("an assignment must name its session")
        await self._ensure_running()
        # This fork's own name.
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
        with contextlib.suppress(PrototypeUnavailable, OSError, asyncio.TimeoutError):
            # Wait for the report itself rather than for a length of time.
            self._status_arrived.clear()
            await self._request({"command": "status"})
            await asyncio.wait_for(self._status_arrived.wait(), timeout=1.0)
        return self.status()


__all__ = ["PrototypeClient", "PrototypeUnavailable", "SessionExit", "prototype_command"]
