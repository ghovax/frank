"""The prototype: one process that has paid the import, so every session is a copy of it.

A session worker costs about 264 MB and five seconds, and almost all of both is importing
the runtime — LangChain, LiteLLM, the model clients, tree-sitter — which is identical for
every session regardless of which agent it will run. The old answer was to keep a couple of
started-but-unassigned workers around and hand one out, which hides the cost for the first
two sessions and pays it in full for the third.

This is the other answer. The prototype imports the runtime once, freezes its heap, and
parks. Asked for a session, it forks; the child inherits the whole address space
copy-on-write and is serving in about 60 milliseconds with roughly 12 MB of its own. Twelve
sessions cost 406 MB instead of 3.4 GB, and the thirteenth costs the same as the second.

    frankd  ──spawns──▶  prototype  ──forks──▶  session worker
     14.8 MB              263.8 MB               ≈ 12 MB
     no runtime           runtime + gc.freeze()  setsid, own socket, own signals

It runs no agent, holds no registry, and makes no decisions. It knows nothing about
permission modes, session trees or tokens — an assignment is an opaque dictionary it passes
to the child. It does exactly one thing the daemon cannot do for itself, because the daemon
must never import the runtime and the thing that forks must be the thing that has already
paid for it. It reports child exits for the same structural reason: the daemon cannot
`waitpid` a process it did not fork, and the prototype is the only process in a position to
see them.

**One worker, one session, one activation.** A child is never reused and never returns here.
A slept session's next worker is a fresh copy of this image, not a recycled one, so there is
no path by which one session's state can reach another's.

## The three conditions

Forking a live Python interpreter is safe only under conditions this file has to hold, not
hope for:

1. **Single-threaded at the moment of the fork.** `fork(2)` carries only the calling thread,
   so a lock another thread held stays locked forever in the child. On macOS the failure is
   louder and more confusing: the child aborts inside the Objective-C runtime complaining
   about `+[__NSPlaceholderSet initialize]`, which reads like a CoreFoundation problem and is
   not one. This is asserted before every fork with :func:`native_thread_count`, and the
   prototype refuses rather than forking unsafely — the invariant has been broken once
   already, by an import-time HTTP call nobody thought of as concurrency.

   It is also why there is no event loop here and no `run_in_executor`. A blocking wait on a
   descriptor needs neither, and the old worker's `run_in_executor(None, sys.stdin.readline)`
   made the process multi-threaded for no reason at all.

2. **`gc.freeze()` before forking.** Without it the child's first cyclic collection walks
   every tracked object, dirties the page it lives on, and un-shares 98 MB of what was
   supposed to be shared — the saving silently drops from 88% to about a third of that, with
   nothing breaking to tell you. `gc.freeze` moves everything into a permanent generation the
   collector does not touch, so the pages stay clean.

3. **The child takes its own signals and leads its own process session.** `setsid` is what
   `peer_identity.session_for_process` attributes a caller by, and default signal handling is
   what stops an inherited handler from killing a child the instant it is signalled.

## What must never be imported here

`frank.computer` pulls in PyObjC, which initialises CoreFoundation, which is the one thing
that genuinely cannot survive a fork on macOS. The invariant that it is only ever imported
inside a function is a rule this process depends on, and the reason it exists. The same goes
for network calls at import, for condition 1.
"""

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
from typing import Any, Optional

logger = logging.getLogger("frank.prototype")


def native_thread_count() -> int:
    """How many threads this process has, as the kernel counts them.

    Deliberately not `threading.enumerate()`, and this distinction is the whole reason the
    function exists. `threading.enumerate` reports only threads CPython created; a native
    thread started inside a C extension — an HTTP client's resolver pool, a framework's run
    loop — is invisible to it. `fork(2)` cares about the kernel's count, and so does the
    Objective-C runtime that aborts the child.

    Answers ``0`` when it cannot tell, which callers treat as "cannot prove it is safe".
    """
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
    """The macOS answer, through `task_threads`.

    Bound lazily and locally: `ctypes` finding libSystem is not free, and this must not run
    at import on a platform where it is meaningless."""
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
        # The port array is allocated in this task's address space by the kernel and is ours
        # to release. Leaking one per fork would be a slow leak in the longest-lived process.
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
    """Resolve the system proxy configuration once, out of process, and export the answer.

    This exists because of a defect that is invisible in Python and fatal to the fork. On macOS
    `urllib.request.getproxies()` is `getproxies_environment() or getproxies_macosx_sysconf()`,
    and the second reaches SystemConfiguration through `_scproxy`, which leaves **two native
    threads in the process permanently**. `litellm` builds a module-level `httpx.Client()` at
    import, `httpx.Client()` calls `getproxies()`, and so importing the runtime took the
    prototype from one thread to three. A multi-threaded process cannot legally `fork()`, and
    the children aborted inside the Objective-C runtime with a message naming
    `+[__NSPlaceholderSet initialize]` — which reads like the CoreFoundation verdict and is a
    different thing entirely.

    This is the second instance of the class the hazard register predicted, and the first that
    came from a third-party package rather than our own code. A rule against network calls at
    import can only ever cover this repository; it cannot see inside `litellm`.

    The fix is to make `getproxies_environment()` truthy before anything imports `litellm`, so
    the `or` short-circuits and `_scproxy` is never reached. Resolving the *real* configuration
    in a throwaway subprocess and exporting it preserves proxy behaviour exactly — the threads
    are created in a process that is about to exit. Where no proxy is configured, `no_proxy=*`
    is both truthy and semantically correct, and verified not to invent a proxy: `httpx.Client()`
    mounts nothing under it.

    A no-op when the caller already set any of these, because then the short-circuit already
    happens and their configuration is not ours to second-guess. Children inherit the result
    through the environment, which matters — a forked session calls `getproxies()` too.
    """
    if any(os.environ.get(name) for name in _PROXY_VARIABLES):
        return {}
    resolved: dict[str, str] = {}
    try:
        completed = subprocess.run(
            [sys.executable, "-c",
             "import json,urllib.request;print(json.dumps(urllib.request.getproxies()))"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if completed.returncode == 0:
            resolved = json.loads(completed.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        # A resolver that will not run tells us nothing about the system's proxies, and
        # guessing would be worse than the honest default below.
        resolved = {}

    # `getproxies()` keys by scheme — `http`, `https`, `no` — and the environment spells each
    # of those `<key>_proxy`, so the mapping is the same for the bypass list as for the rest.
    exported = {f"{scheme}_proxy": address for scheme, address in resolved.items() if address}
    if not exported:
        exported["no_proxy"] = "*"
    os.environ.update(exported)
    return exported


def session_command(assignment_fd: int, ready_fd: int) -> list[str]:
    """How a session worker is launched: a re-exec of *this* executable.

    The same reasoning as :func:`frank.daemon.prototype.prototype_command`, and now it carries
    more weight. A session used to inherit the prototype's code signature by being a fork of
    it; it execs now, so the signature comes from the image it execs — which is why that image
    has to be this one and not the interpreter. Anything else would be a second code identity
    and a second Accessibility prompt per session.

    The two descriptors are passed as numbers because that is all an `exec` boundary carries.
    The assignment itself is not: it holds capability tokens, and `argv` is readable by any
    process on the machine."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "session", str(assignment_fd), str(ready_fd)]
    return [sys.executable, "-m", "frank", "session", str(assignment_fd), str(ready_fd)]


def _load_runtime() -> None:
    """Import everything a session will need, so the fork has nothing left to pay for.

    Importing the session executor is what drags in the real graph — the runtime, the tools,
    the model clients, the A2A machinery. `serve` is imported for the same reason: it is what
    the child calls, and an import in the child would be an import per session."""
    import uvicorn  # noqa: F401 — imported for its side effect on sys.modules

    import frank.worker.serve  # noqa: F401
    import frank.worker.server  # noqa: F401
    import frank.worker.session  # noqa: F401


def _freeze() -> None:
    """Collect, then move everything reachable out of the collector's reach.

    The collect first is not decoration: freezing an uncollected heap would make garbage
    permanent, so the order is collect-then-freeze and the saving depends on it."""
    gc.collect()
    gc.freeze()


class Prototype:
    """The parked image, and the fork loop that copies it.

    Everything is descriptor-driven and single-threaded. The selector watches three kinds of
    thing: the listening socket (the daemon connecting, including reconnecting after its own
    restart), the daemon's connection (fork requests), and one pipe per in-flight fork (a
    child reporting that it is serving). Child deaths arrive as `SIGCHLD` on a self-pipe,
    which is the standard way to make a signal wait alongside descriptors without a thread.
    """

    def __init__(self, socket_path: Path) -> None:
        self._socket_path = socket_path
        self._selector = selectors.DefaultSelector()
        self._listener: Optional[socket.socket] = None
        self._control: Optional[socket.socket] = None
        self._control_buffer = b""
        # pid -> (session id, fork token), so an exit can be reported both as the session it
        # was and as the *particular* process it was. The token is what lets the daemon tell a
        # dead worker's exit from its replacement's: a session is forked afresh every turn, so
        # the session id alone names a queue of processes rather than one.
        self._children: dict[int, tuple[str, str]] = {}
        # ready-pipe read end -> (session id, fork token, buffer)
        self._pending: dict[int, tuple[str, str, bytearray]] = {}
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

    def _install_child_wakeup(self) -> None:
        """Turn `SIGCHLD` into a readable descriptor.

        `set_wakeup_fd` needs a handler installed for the signal to be delivered at all, and
        the handler itself does nothing: the wakeup byte is the message. This is a signal
        *this* process needs, unlike the ones a library must never seize — a prototype that
        cannot notice its children dying is a prototype that reports live sessions forever."""
        read_end, write_end = os.pipe()
        os.set_blocking(read_end, False)
        os.set_blocking(write_end, False)
        self._wakeup_read, self._wakeup_write = read_end, write_end
        signal.signal(signal.SIGCHLD, lambda *_: None)
        signal.set_wakeup_fd(write_end)
        self._selector.register(read_end, selectors.EVENT_READ, self._on_wakeup)

    def run(self) -> int:
        """Park, and serve fork requests until told to stop."""
        logger.info(
            "prototype ready: pid=%d threads=%d frozen=%d socket=%s",
            os.getpid(), native_thread_count(), gc.get_freeze_count(), self._socket_path,
        )
        while not self._stopping:
            try:
                events = self._selector.select(timeout=None)
            except InterruptedError:
                continue
            except OSError:
                if self._stopping:
                    break
                raise
            for key, _mask in events:
                key.data(key.fileobj)
        return 0

    def stop(self) -> None:
        self._stopping = True

    def close(self) -> None:
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
        # Exactly one client at a time. A second connection means the daemon restarted and
        # the old one is stale, so the newest wins rather than the oldest holding the slot.
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
            logger.info("daemon detached")
            self._drop_control()
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
        """Report to the daemon, if it is attached.

        Best effort by design: a daemon that has gone away must not stop the prototype from
        serving the sessions it already forked, and the reports it missed are re-derivable
        from the registry when it comes back."""
        if self._control is None:
            return
        try:
            self._control.sendall((json.dumps(payload) + "\n").encode())
        except OSError:
            self._drop_control()

    # Making a session, and everything the child does before it is one.

    def _fork_session(self, fork: str, assignment: dict) -> None:
        """Fork one session process.

        `fork` names *this* child and is echoed on every report about it. The session id names
        the conversation, which outlives any one process — so it cannot tell the daemon which
        incarnation a report describes, and a report is useless to a waiter that cannot.
        """
        session_id = str(assignment.get("session_id") or "")

        ready_read, ready_write = os.pipe()
        # Written before the fork, so the child finds its assignment already in the pipe and
        # neither process has to coordinate a handover between `fork` and `exec` — the window
        # where almost nothing is legal to do. An assignment is well under the pipe buffer;
        # a write that would block here is a bug in what is being assigned, not in the size.
        assignment_read, assignment_write = os.pipe()
        try:
            os.write(assignment_write, json.dumps(assignment).encode())
        except OSError as error:
            for descriptor in (ready_read, ready_write, assignment_read, assignment_write):
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            logger.error("could not write the assignment for %s: %s", session_id, error)
            self._send({"event": "failed", "fork": fork, "session_id": session_id, "reason": str(error)})
            return
        os.close(assignment_write)
        # `exec` closes everything marked close-on-exec, which is the default for a pipe. These
        # two have to cross it, and their numbers are what the child is told on its command line.
        os.set_inheritable(assignment_read, True)
        os.set_inheritable(ready_write, True)

        try:
            pid = os.fork()
        except OSError as error:
            for descriptor in (ready_read, ready_write, assignment_read):
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            logger.error("could not fork: %s", error)
            self._send({"event": "failed", "fork": fork, "session_id": session_id, "reason": str(error)})
            return

        if pid == 0:
            os._exit(self._become_session(assignment_read, ready_read, ready_write))

        os.close(ready_write)
        os.close(assignment_read)
        os.set_blocking(ready_read, False)
        self._children[pid] = (session_id, fork)
        self._pending[ready_read] = (session_id, fork, bytearray())
        self._selector.register(ready_read, selectors.EVENT_READ, self._on_ready_readable)

    def _become_session(self, assignment_read: int, ready_read: int, ready_write: int) -> int:
        """Everything the child does between `fork` and `exec`. Runs only in the child.

        Deliberately short, and every call in it async-signal-safe. Between those two points a
        forked child may touch almost nothing — it holds copies of locks no thread here will
        ever release — so this does the handful of things that must happen before the image is
        replaced and then replaces it. The session itself begins on the other side of the
        `exec`, in :mod:`frank.worker.session_entry`.

        It does not return on success. Anything raising would otherwise carry on into the
        parent's code path holding the child's copy of the world, so it is wrapped: a child
        that cannot start must exit, not return."""
        try:
            # Its own process session, which is what kernel-attested peer identity is keyed
            # on and what lets a reap signal the session's whole shell subtree at once.
            os.setsid()
            # Default handling for everything the parent may have touched. An inherited
            # handler that calls `sys.exit` kills this child the first time it is signalled,
            # and the wakeup descriptor belongs to a process this one is no longer part of.
            # `exec` resets handlers anyway; doing it here covers the window before it.
            signal.set_wakeup_fd(-1)
            for received in (signal.SIGCHLD, signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                with contextlib.suppress(OSError, ValueError):
                    signal.signal(received, signal.SIG_DFL)
            # The parent's descriptors are not this process's business, and holding the
            # listening socket open would make the prototype's own shutdown never complete.
            os.close(ready_read)
            self._close_inherited()
            command = session_command(assignment_read, ready_write)
            os.execv(command[0], command)
            # `execv` does not return. Reaching here at all means it failed without raising,
            # which it cannot, but a fall-through into the parent's loop would be far worse
            # than an exit.
            return 1
        except BaseException:  # noqa: BLE001 — a child that cannot start must exit, not unwind
            with contextlib.suppress(Exception):
                logging.getLogger("frank.worker").exception("Session process could not exec")
            return 1

    def _close_inherited(self) -> None:
        """Drop the parent's sockets and pipes. Runs only in the child."""
        for descriptor in (self._wakeup_read, self._wakeup_write):
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
        for descriptor in list(self._pending):
            with contextlib.suppress(OSError):
                os.close(descriptor)
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
        # The exit report follows on its own; this is what stops the daemon waiting for it.
        reason = str(payload.get("reason") or "the session process ended before it was serving")
        self._send({"event": "failed", "fork": fork, "session_id": session_id, "reason": reason})

    # Noticing children die, and telling the daemon.

    def _on_wakeup(self, descriptor: int) -> None:
        with contextlib.suppress(BlockingIOError, InterruptedError, OSError):
            os.read(descriptor, 4096)
        self._reap()

    def _reap(self) -> None:
        """Collect every child that has ended and report each as the session it was.

        A loop rather than a single `waitpid`, because signals coalesce: two children exiting
        close together deliver one `SIGCHLD`, and taking one per wakeup would leave the other
        a zombie and its session marked live forever."""
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            except OSError:
                return
            if pid == 0:
                return
            session_id, fork = self._children.pop(pid, ("", ""))
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
    # The same file the daemon writes, because this process and every session forked from it
    # are where turns actually run — and `logging.basicConfig` without a handler writes to a
    # stderr that nothing keeps. `_fail` in `worker/turn.py` has always called
    # `logger.exception("Agent turn failed")`; it went nowhere, so a turn that died reached the
    # interface as "the raw details were written to the server log" when no such line existed.
    from frank.base.paths import log_file_path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(log_file_path("frankd"))],
    )
    from frank.base.paths import prototype_socket_path

    # Strictly before `_load_runtime`. Importing `litellm` is what calls `getproxies()`, and
    # once SystemConfiguration has been touched the threads are permanent — settling the
    # environment afterwards would be too late by one import.
    # Still settled here even though a session now execs and calls `getproxies()` for itself.
    # It is inherited through the environment, so doing it once in this process spares every
    # session the SystemConfiguration lookup rather than paying it per session.
    exported = _settle_proxy_environment()
    if exported:
        logger.info("proxy environment settled out of process: %s", sorted(exported))

    # No runtime import, no `gc.freeze()`, and no single-threaded assertion. All three existed
    # to make a fork *without* an exec survivable: the import so the child inherited a warm
    # image, the freeze so its first collection did not un-share that image, and the assertion
    # because a multi-threaded process cannot fork safely into arbitrary code. A child that
    # execs inherits none of it and is bound by none of it — between `fork` and `exec` it makes
    # only async-signal-safe calls, which is legal from any number of threads. What is left
    # here is a supervisor: it forks, it execs, it waits, and it reports what happened.

    prototype = Prototype(prototype_socket_path())
    prototype.start()
    # SIGTERM must land as an orderly stop rather than as an abort, so the socket is unlinked
    # and the daemon does not find a stale path on the next start. This is a composition
    # root — the process's own entry point — which is the one place that may take signals.
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
