"""The process that starts session workers and keeps a few started ahead of demand, so a session begins in milliseconds."""

from __future__ import annotations

import contextlib
import gc
import json
import logging
import os
import selectors
import signal
import socket
import subprocess
import sys
from pathlib import Path

from dataclasses import dataclass
from typing import Any, Optional

from frank.base.fork_protocol import StartFailure
from frank.base.tuning import Tunable, active_tuning

logger = logging.getLogger("frank.prototype")


def native_thread_count() -> int:
    """How many threads this process has as the kernel counts them, which includes threads a C extension started."""
    if sys.platform == "darwin":
        return _mach_thread_count()
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("Threads:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return 0
    return 0


def _mach_thread_count() -> int:
    """The macOS answer through `task_threads`, bound lazily so `ctypes` is not paid for at import."""
    import ctypes
    import ctypes.util

    try:
        library_path = ctypes.util.find_library("System") or "/usr/lib/libSystem.dylib"
        library = ctypes.CDLL(library_path, use_errno=True)
        mach_task_self = library.mach_task_self
        mach_task_self.restype = ctypes.c_uint
        task_threads = library.task_threads
        task_threads.restype = ctypes.c_int
        threads = ctypes.POINTER(ctypes.c_uint)()
        count = ctypes.c_uint(0)
        if task_threads(mach_task_self(), ctypes.byref(threads), ctypes.byref(count)) != 0:
            return 0
        total = int(count.value)
        # The port array is allocated in this task and ours to release; leaking one per fork would leak here longest.
        library.vm_deallocate(
            mach_task_self(),
            ctypes.cast(threads, ctypes.c_void_p),
            ctypes.c_size_t(total * ctypes.sizeof(ctypes.c_uint)),
        )
        return total
    except (OSError, AttributeError, ValueError):
        return 0


_PROXY_VARIABLES = ("http_proxy", "https_proxy", "all_proxy", "no_proxy",
                   "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


def _settle_proxy_environment() -> dict[str, str]:
    """Resolve the system proxy configuration once out of process and export it, since resolving it in process spawns threads that break the fork."""
    if any(os.environ.get(name) for name in _PROXY_VARIABLES):
        return {}
    resolved: dict[str, str] = {}
    # Frozen, `sys.executable` is the `frank` binary rather than an interpreter, so the packaged build answers a role instead of `-c`.
    if getattr(sys, "frozen", False):
        command = [sys.executable, "read-proxies"]
    else:
        command = [sys.executable, "-c",
                   "import json,urllib.request;print(json.dumps(urllib.request.getproxies()))"]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=10, check=False,
        )
        if completed.returncode == 0:
            resolved = json.loads(completed.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        # A resolver that will not run tells us nothing, and guessing would be worse than the honest default below.
        resolved = {}

    # `getproxies()` keys by scheme and the environment spells each `<key>_proxy`, so one mapping serves them all.
    exported = {f"{scheme}_proxy": address for scheme, address in resolved.items() if address}
    if not exported:
        exported["no_proxy"] = "*"
    os.environ.update(exported)
    return exported


def session_command(assignment_fd: int, ready_fd: int, lifeline_fd: int) -> list[str]:
    """How a session worker is launched: a re-exec of this executable, so the child carries its own code signature."""
    numbers = [str(assignment_fd), str(ready_fd), str(lifeline_fd)]
    if getattr(sys, "frozen", False):
        return [sys.executable, "session", *numbers]
    return [sys.executable, "-m", "frank", "session", *numbers]


def _load_runtime() -> None:
    """Import everything a session will need, so the fork has nothing left to pay for."""
    import uvicorn  # noqa: F401 — imported for its side effect on sys.modules

    import frank.worker.serve  # noqa: F401
    import frank.worker.server  # noqa: F401
    import frank.worker.session  # noqa: F401


def _freeze() -> None:
    """Collect before freezing, because freezing an uncollected heap would make its garbage permanent."""
    gc.collect()
    gc.freeze()


@dataclass
class _ParkedWorker:
    """A worker started ahead of demand, blocked on an assignment; writing one there is what turns it into a session."""

    pid: int
    assignment_write: int
    ready_read: int
    lifeline_write: int


@dataclass
class _SessionChild:
    """A worker that has been given a session, with `fork` naming this incarnation and the lifeline holding its life."""

    session_id: str
    fork: str
    lifeline_write: int


class Prototype:
    """The parked image and the fork loop that copies it, descriptor-driven and single-threaded."""

    def __init__(self, socket_path: Path) -> None:
        self._socket_path = socket_path
        self._selector = selectors.DefaultSelector()
        self._listener: Optional[socket.socket] = None
        self._control: Optional[socket.socket] = None
        self._control_buffer = b""
        # Process id to child, so an exit reports both the session it was and the particular process it was.
        self._children: dict[int, _SessionChild] = {}
        # ready-pipe read end -> (session id, fork token, buffer)
        self._pending: dict[int, tuple[str, str, bytearray]] = {}
        # Workers started before anyone asked, each already past the runtime import and blocked on its assignment.
        self._parked: list[_ParkedWorker] = []
        self._refill_wanted = False
        self._wakeup_read = -1
        self._wakeup_write = -1
        self._stopping = False

    # Binding the socket, parking on it, and tearing both down.

    def start(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self._socket_path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self._socket_path))
        listener.listen(8)
        listener.setblocking(False)
        os.chmod(self._socket_path, 0o600)
        self._listener = listener
        self._selector.register(listener, selectors.EVENT_READ, self._accept)
        self._install_child_wakeup()
        # Ask for the pool now, so the first session of the day is as quick as the rest.
        self._want_refill()

    def _install_child_wakeup(self) -> None:
        """Turn `SIGCHLD` into a readable descriptor, with a handler that does nothing because the wakeup byte is the message."""
        read_end, write_end = os.pipe()
        os.set_blocking(read_end, False)
        os.set_blocking(write_end, False)
        self._wakeup_read, self._wakeup_write = read_end, write_end
        signal.signal(signal.SIGCHLD, lambda *_: None)
        signal.set_wakeup_fd(write_end)
        self._selector.register(read_end, selectors.EVENT_READ, self._on_wakeup)

    def _interrupt_wait(self) -> None:
        """Wake a `select` that is parked with no timeout, so deferred work happens promptly."""
        if self._wakeup_write >= 0:
            with contextlib.suppress(OSError):
                os.write(self._wakeup_write, b"\x00")

    def run(self) -> int:
        """Park, and serve fork requests until told to stop."""
        logger.info(
            "prototype ready: pid=%d threads=%d frozen=%d socket=%s",
            os.getpid(), native_thread_count(), gc.get_freeze_count(), self._socket_path,
        )
        while not self._stopping:
            try:
                events = self._selector.select(timeout=0 if self._refill_wanted else None)
            except InterruptedError:
                continue
            except OSError:
                if self._stopping:
                    break
                raise
            for key, _mask in events:
                key.data(key.fileobj)
            # After the requests, never before: the one who asked has already been answered.
            if self._refill_wanted and not self._stopping:
                self._refill_pool()
        return 0

    def stop(self) -> None:
        self._stopping = True

    def close(self) -> None:
        # The lifelines first, so ending this process visibly starts every worker's shutdown rather than merely implying it.
        for parked in self._parked:
            with contextlib.suppress(OSError):
                os.close(parked.lifeline_write)
        for child in self._children.values():
            with contextlib.suppress(OSError):
                os.close(child.lifeline_write)
        signal.set_wakeup_fd(-1)
        for descriptor in (self._wakeup_read, self._wakeup_write):
            if descriptor >= 0:
                with contextlib.suppress(OSError, KeyError):
                    self._selector.unregister(descriptor)
                with contextlib.suppress(OSError):
                    os.close(descriptor)
        if self._control is not None:
            self._drop_control()
        if self._listener is not None:
            with contextlib.suppress(OSError, KeyError):
                self._selector.unregister(self._listener)
            with contextlib.suppress(OSError):
                self._listener.close()
        with contextlib.suppress(OSError):
            self._socket_path.unlink()
        self._selector.close()

    # The daemon's connection, and the commands that arrive on it.

    def _accept(self, listener) -> None:
        try:
            connection, _ = listener.accept()
        except OSError:
            return
        connection.setblocking(False)
        # Exactly one client at a time, and the newest wins, as everywhere else here.
        if self._control is not None:
            self._drop_control()
        self._control = connection
        self._control_buffer = b""
        self._selector.register(connection, selectors.EVENT_READ, self._on_control_readable)
        logger.info("daemon attached")

    def _drop_control(self) -> None:
        if self._control is None:
            return
        with contextlib.suppress(OSError, KeyError):
            self._selector.unregister(self._control)
        with contextlib.suppress(OSError):
            self._control.close()
        self._control = None
        self._control_buffer = b""

    def _on_control_readable(self, connection) -> None:
        try:
            chunk = connection.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            chunk = b""
        if not chunk:
            # The daemon's end of this socket is the prototype's own lifeline, and this is it closing.
            logger.info("the daemon is gone; stopping")
            self._drop_control()
            self.stop()
            return
        self._control_buffer += chunk
        while b"\n" in self._control_buffer:
            line, self._control_buffer = self._control_buffer.split(b"\n", 1)
            if line.strip():
                self._handle_command(line)

    def _handle_command(self, line: bytes) -> None:
        try:
            message = json.loads(line.decode())
        except (ValueError, UnicodeDecodeError):
            logger.error("ignoring a malformed command")
            return
        command = str(message.get("command") or "")
        if command == "fork":
            self._fork_session(str(message.get("fork") or ""), message.get("assignment") or {})
        elif command == "status":
            self._send({
                "event": "status",
                "pid": os.getpid(),
                "threads": native_thread_count(),
                "frozen": gc.get_freeze_count(),
                "children": len(self._children),
            })
        elif command == "shutdown":
            self.stop()
        else:
            logger.error("ignoring an unknown command: %s", command)

    def _send(self, payload: dict[str, Any]) -> None:
        """Report to the daemon if it is attached, best effort, since the reports are re-derivable from the registry."""
        if self._control is None:
            return
        try:
            self._control.sendall((json.dumps(payload) + "\n").encode())
        except OSError:
            self._drop_control()

    # Making a session, and everything the child does before it is one.

    def _start_worker(self) -> Optional[_ParkedWorker]:
        """Fork and exec one worker and leave it blocked on an assignment, which is the expensive half and needs no session."""
        ready_read, ready_write = os.pipe()
        assignment_read, assignment_write = os.pipe()
        # The lifeline carries nothing: the child stops when the last copy of the write end closes, which is this process ending.
        lifeline_read, lifeline_write = os.pipe()
        # These three descriptors have to cross the `exec`, while every other worker's stay close-on-exec.
        os.set_inheritable(assignment_read, True)
        os.set_inheritable(ready_write, True)
        os.set_inheritable(lifeline_read, True)

        try:
            pid = os.fork()
        except OSError:
            for descriptor in (ready_read, ready_write, assignment_read, assignment_write,
                               lifeline_read, lifeline_write):
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            logger.error("could not fork", exc_info=True)
            return None

        if pid == 0:
            with contextlib.suppress(OSError):
                os.close(assignment_write)
            # The child must not hold its own lifeline's write end, or it would keep itself alive forever.
            with contextlib.suppress(OSError):
                os.close(lifeline_write)
            os._exit(self._become_session(assignment_read, ready_read, ready_write, lifeline_read))

        os.close(ready_write)
        os.close(assignment_read)
        os.close(lifeline_read)
        os.set_blocking(ready_read, False)
        return _ParkedWorker(
            pid=pid, assignment_write=assignment_write, ready_read=ready_read,
            lifeline_write=lifeline_write,
        )

    def _refill_pool(self) -> None:
        """Bring the pool back up to strength, once the loop has nothing else to answer."""
        self._refill_wanted = False
        target = self._warm_workers()
        while len(self._parked) < target:
            worker = self._start_worker()
            if worker is None:
                logger.warning("could not start a warm worker; the pool is short (%d of %d)",
                               len(self._parked), target)
                return
            self._parked.append(worker)
            logger.info("warm worker started (pid %d); importing the runtime — pool %d of %d",
                        worker.pid, len(self._parked), target)

    def _warm_workers(self) -> int:
        """How many to keep, read afresh so a configuration change takes effect on the next refill."""
        return active_tuning().amount(Tunable.warm_workers)

    def _want_refill(self) -> None:
        """Ask for a refill on the next turn of the loop, rather than doing one here."""
        self._refill_wanted = True
        self._interrupt_wait()

    def _fork_session(self, fork: str, assignment: dict) -> None:
        """Give one worker a session and tell the daemon which process became it, named by fork rather than by session."""
        session_id = str(assignment.get("session_id") or "")

        warm = bool(self._parked)
        worker = self._parked.pop(0) if warm else self._start_worker()
        if warm:
            logger.info("session %s takes warm worker pid %d — %d left parked",
                        session_id or "?", worker.pid if worker else -1, len(self._parked))
        else:
            logger.info("session %s found no warm worker and starts its own; it waits for the "
                        "runtime import", session_id or "?")
        if worker is None:
            self._send({
                "event": "failed", "fork": fork, "session_id": session_id,
                "reason": StartFailure.FORK_FAILED, "detail": "could not start a worker",
            })
            return

        # An assignment is well under the pipe buffer, and closing is what ends the child's read.
        try:
            os.write(worker.assignment_write, json.dumps(assignment).encode())
            os.close(worker.assignment_write)
        except OSError as error:
            with contextlib.suppress(OSError):
                os.close(worker.assignment_write)
            with contextlib.suppress(OSError):
                os.close(worker.ready_read)
            logger.error("could not write the assignment for %s", session_id, exc_info=True)
            self._send({
                "event": "failed", "fork": fork, "session_id": session_id,
                "reason": StartFailure.ASSIGNMENT_UNWRITABLE, "detail": str(error),
            })
            self._want_refill()
            return

        self._children[worker.pid] = _SessionChild(
            session_id=session_id, fork=fork, lifeline_write=worker.lifeline_write,
        )
        self._pending[worker.ready_read] = (session_id, fork, bytearray())
        self._selector.register(worker.ready_read, selectors.EVENT_READ, self._on_ready_readable)
        self._want_refill()

    def _become_session(
        self, assignment_read: int, ready_read: int, ready_write: int, lifeline_read: int
    ) -> int:
        """Everything the child does between `fork` and `exec`, kept short and async-signal-safe."""
        try:
            # Its own process session, which peer identity is keyed on and which lets a reap signal the whole shell subtree.
            os.setsid()
            # Default handling for everything the parent may have touched, since an inherited handler would kill this child.
            signal.set_wakeup_fd(-1)
            for received in (signal.SIGCHLD, signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                with contextlib.suppress(OSError, ValueError):
                    signal.signal(received, signal.SIG_DFL)
            # The parent's descriptors are not this process's business, and the listening socket would block its shutdown.
            os.close(ready_read)
            self._close_inherited()
            command = session_command(assignment_read, ready_write, lifeline_read)
            os.execv(command[0], command)
            # `execv` does not return, so reaching here at all must exit rather than fall through into the parent's loop.
            return 1
        except BaseException:  # noqa: BLE001 — a child that cannot start must exit, not unwind
            with contextlib.suppress(Exception):
                logging.getLogger("frank.worker").exception("Session process could not exec")
            return 1

    def _close_inherited(self) -> None:
        """Drop the parent's sockets and pipes, covering the window before `exec` closes them and stating the rule outright."""
        for descriptor in (self._wakeup_read, self._wakeup_write):
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
        for descriptor in list(self._pending):
            with contextlib.suppress(OSError):
                os.close(descriptor)
        for parked in self._parked:
            for descriptor in (parked.assignment_write, parked.ready_read, parked.lifeline_write):
                with contextlib.suppress(OSError):
                    os.close(descriptor)
        for child in self._children.values():
            with contextlib.suppress(OSError):
                os.close(child.lifeline_write)
        for connection in (self._control, self._listener):
            if connection is not None:
                with contextlib.suppress(OSError):
                    connection.close()

    def _on_ready_readable(self, descriptor: int) -> None:
        session_id, fork, buffer = self._pending[descriptor]
        try:
            chunk = os.read(descriptor, 4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            chunk = b""
        if chunk:
            buffer.extend(chunk)
            if b"\n" not in buffer:
                return
        self._finish_ready(descriptor, session_id, fork, bytes(buffer))

    def _finish_ready(self, descriptor: int, session_id: str, fork: str, raw: bytes) -> None:
        with contextlib.suppress(OSError, KeyError):
            self._selector.unregister(descriptor)
        with contextlib.suppress(OSError):
            os.close(descriptor)
        self._pending.pop(descriptor, None)
        line = raw.split(b"\n", 1)[0]
        payload: dict[str, Any] = {}
        if line.strip():
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                payload = json.loads(line.decode())
        if payload.get("ready"):
            self._send({
                "event": "ready",
                "fork": fork,
                "session_id": session_id,
                "pid": int(payload.get("pid") or 0),
            })
            return
        # The pipe closing with nothing on it means the child died before it could answer.
        reason = str(payload.get("reason") or StartFailure.EXITED_BEFORE_SERVING)
        self._send({
            "event": "failed", "fork": fork, "session_id": session_id,
            "reason": reason, "detail": str(payload.get("detail") or ""),
        })

    # Noticing children die, and telling the daemon.

    def _on_wakeup(self, descriptor: int) -> None:
        with contextlib.suppress(BlockingIOError, InterruptedError, OSError):
            os.read(descriptor, 4096)
        self._reap()

    def _reap(self) -> None:
        """Collect every child that has ended, in a loop, because signals coalesce and one per wakeup would leave zombies."""
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            except OSError:
                return
            if pid == 0:
                return
            # A worker that died while parked belonged to no session, so there is only a gap in the pool to close.
            parked = next((worker for worker in self._parked if worker.pid == pid), None)
            if parked is not None:
                self._parked.remove(parked)
                for descriptor in (parked.assignment_write, parked.ready_read,
                                   parked.lifeline_write):
                    with contextlib.suppress(OSError):
                        os.close(descriptor)
                logger.warning("a parked worker (pid %d) ended before it was used", pid)
                self._want_refill()
                continue
            child = self._children.pop(pid, None)
            session_id, fork = (child.session_id, child.fork) if child else ("", "")
            # Drop our end of the dead child's lifeline, so it is not held for the life of the prototype.
            if child is not None:
                with contextlib.suppress(OSError):
                    os.close(child.lifeline_write)
            if os.WIFSIGNALED(status):
                code, signal_number = -1, os.WTERMSIG(status)
            else:
                code, signal_number = os.WEXITSTATUS(status), 0
            logger.info("session %s (pid %d) ended: code=%d signal=%d", session_id or "?", pid, code, signal_number)
            self._send({
                "event": "exited",
                "fork": fork,
                "session_id": session_id,
                "pid": pid,
                "code": code,
                "signal": signal_number,
            })


def main() -> int:
    # The same log file the daemon writes, because this process and its sessions are where turns actually run.
    from frank.base.paths import log_file_path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(log_file_path("frankd"))],
    )
    from frank.base.paths import prototype_socket_path

    # Strictly before the runtime import, since importing `litellm` calls `getproxies()` and the threads are then permanent.
    exported = _settle_proxy_environment()
    if exported:
        logger.info("proxy environment settled out of process: %s", sorted(exported))

    # No runtime import, freeze or single-threaded assertion, all of which existed to make a fork without an exec survivable.

    prototype = Prototype(prototype_socket_path())
    prototype.start()
    # SIGTERM must land as an orderly stop so the socket is unlinked; an entry point is the one place that may take signals.
    for received in (signal.SIGTERM, signal.SIGINT):
        signal.signal(received, lambda *_: prototype.stop())
    try:
        return prototype.run()
    except KeyboardInterrupt:
        return 0
    finally:
        prototype.close()


__all__ = ["Prototype", "main", "native_thread_count"]


if __name__ == "__main__":
    sys.exit(main())
