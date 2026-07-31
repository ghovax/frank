"""The prototype: the process that starts session workers, and keeps some started already.

Almost all of what a session worker costs is importing the runtime — LangChain, LiteLLM, the
model clients, tree-sitter — about two and a half seconds, and identical for every session
whatever agent it will run. Nothing about that work depends on which session it is for, which
is the fact this file is built around.

So a worker is started before anyone asks for one. The import is still paid in full; it is
simply paid by a process nobody is waiting for, ahead of the request that needs it. Three
parts, each doing one thing:

- **`frankd`** spawns the prototype and never imports the runtime itself.
- **The prototype** starts workers ahead of demand and hands them out. It is the only process
  that can, because a parent cannot `waitpid` a child it did not fork.
- **A worker** forks, execs, imports, then blocks reading an assignment nobody has written.
  Writing one is what turns it from parked into a session, and costs a pipe write.

Two are kept parked, so a second session created while the first is being handed one still
finds a warm worker. When the pool is empty a worker is started on the spot and that session
waits for the import, exactly as every session used to.

An earlier design forked a prototype that had already imported, letting the child inherit the
address space copy-on-write. That is dramatically cheaper in memory and it is why `gc.freeze`
and the single-thread rule below still matter — but a child that only forks and never execs
inherits CoreFoundation state it cannot legally use on macOS, which is what made sessions
crash in `getaddrinfo`. Every child now execs. The pool is what buys the speed back.

It runs no agent, holds no registry, and makes no decisions. It knows nothing about
permission modes, session trees or tokens — an assignment is an opaque dictionary it passes
to the child. It does exactly one thing the daemon cannot do for itself, because the daemon
must never import the runtime. It reports child exits for the same structural reason: the
daemon cannot `waitpid` a process it did not fork, and the prototype is the only process in a
position to see them.

**One worker, one session, one activation.** A child is never reused and never returns here.
A parked worker has no session until it is given one, and having had one it never goes back to
the pool — so there is no path by which one session's state can reach another's.

## Nothing outlives the process that made it

Every process here holds a **lifeline**: a descriptor whose far end only its parent holds, and
which carries no data at all. Reading end-of-file on it means the parent is gone, and the child
stops. The prototype's lifeline is the daemon's control connection; each worker's is a pipe
made for it in `_start_worker`.

This replaces ending children by *signalling* them, which worked only while the parent was
alive and well enough to send a signal — and a parent that has been killed outright, or that
crashed, is neither. A `SIGKILL` to the daemon left a prototype and every worker under it
running: reparented to init, holding sockets nothing would ever call again, and unreapable,
because the only process that could ever have waited on them was the one that had just died.
They accumulated one full set at a time, and the only sign was a machine that was busy for no
reason anyone could point at.

A descriptor asks nothing of the dying process. Closing it is the kernel's doing, so it happens
for every way a process can end, including the ways that run no code — which is what makes the
guarantee hold in exactly the cases the old arrangement could not reach.

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

from dataclasses import dataclass
from typing import Any, Optional

from frank.base.fork_protocol import StartFailure
from frank.base.tuning import Tunable, active_tuning

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


def session_command(assignment_fd: int, ready_fd: int, lifeline_fd: int) -> list[str]:
    """How a session worker is launched: a re-exec of *this* executable.

    The same reasoning as :func:`frank.daemon.prototype.prototype_command`, and now it carries
    more weight. A session used to inherit the prototype's code signature by being a fork of
    it; it execs now, so the signature comes from the image it execs — which is why that image
    has to be this one and not the interpreter. Anything else would be a second code identity
    and a second Accessibility prompt per session.

    The three descriptors are passed as numbers because that is all an `exec` boundary carries.
    The assignment itself is not: it holds capability tokens, and `argv` is readable by any
    process on the machine."""
    numbers = [str(assignment_fd), str(ready_fd), str(lifeline_fd)]
    if getattr(sys, "frozen", False):
        return [sys.executable, "session", *numbers]
    return [sys.executable, "-m", "frank", "session", *numbers]


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


@dataclass
class _ParkedWorker:
    """A worker started ahead of demand, waiting to be told which session it is.

    It has forked, exec'd and imported the runtime; it is blocked reading `assignment_write`'s
    other end. Writing the assignment there and closing it is what turns it into a session.
    """

    pid: int
    assignment_write: int
    ready_read: int
    lifeline_write: int


@dataclass
class _SessionChild:
    """A worker that has been given a session, and this process's hold on its life.

    `fork` names *this* incarnation: a session id names the conversation, which outlives many
    processes, so a report keyed by session id cannot say which one it describes.

    `lifeline_write` is never written to. It is held open for exactly as long as this process
    lives, and its only purpose is to be closed — by `close`, by an exit, or by the kernel when
    this process dies however it dies. The child watches the other end and stops when it sees
    the end-of-file. See :meth:`Prototype._start_worker`.
    """

    session_id: str
    fork: str
    lifeline_write: int


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
        # pid -> the child, so an exit can be reported both as the session it was and as the
        # *particular* process it was. The token is what lets the daemon tell a dead worker's
        # exit from its replacement's: a session is forked afresh every turn, so the session id
        # alone names a queue of processes rather than one.
        self._children: dict[int, _SessionChild] = {}
        # ready-pipe read end -> (session id, fork token, buffer)
        self._pending: dict[int, tuple[str, str, bytearray]] = {}
        # Workers started before anyone asked for one. Each has already forked, exec'd and paid
        # the runtime import, and is blocked reading an assignment that has not been written
        # yet. Handing a session to one is a write down that pipe, which is why a session can
        # start in milliseconds instead of the seconds the import takes.
        #
        # They are kept apart from `_children` deliberately: a parked worker belongs to no
        # session, so its death is a pool refill and not a session that ended.
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
        # Ask for the pool now, so the first session of the day is as quick as the rest. The
        # loop starts them; `start()` returns without waiting for any of it.
        self._want_refill()

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
            # After the requests, never before: a worker started here is for a session nobody
            # has asked for yet, and the one who did ask has already been answered.
            if self._refill_wanted and not self._stopping:
                self._refill_pool()
        return 0

    def stop(self) -> None:
        self._stopping = True

    def close(self) -> None:
        # The lifelines first. Exiting would close them anyway — that is the guarantee the whole
        # mechanism rests on — but closing them here starts every worker's shutdown now rather
        # than after the rest of this teardown, and says plainly that ending this process ends
        # them, instead of leaving it to be inferred from which descriptors happen to be open.
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
        # Exactly one client at a time, and in practice exactly one ever: a daemon spawns its
        # own prototype rather than adopting one, so a second connection would mean two daemons
        # were running. The newest wins, which is the same rule as everywhere else here.
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
            # The daemon's end of this socket is the prototype's own lifeline, and this is it
            # closing. A prototype exists to fork sessions for one daemon and for nothing else:
            # it cannot be reached again once that daemon is gone, because the next daemon
            # spawns its own and unlinks this socket out from under it.
            #
            # So it stops, and the sessions under it stop with it — each through the lifeline
            # it holds, without this process having to signal anything, which is what makes it
            # work when the daemon was killed outright rather than asked to stop. Leaving it
            # running is what left a prototype and a full set of workers behind after every
            # crash, reparented to init and serving sockets nobody would call again.
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

    def _start_worker(self) -> Optional[_ParkedWorker]:
        """Fork and exec one worker, and leave it blocked on an assignment nobody has written.

        This is the expensive half — the fork, the exec, and the runtime import the child does
        before it reads anything — and none of it needs to know which session it is for. Split
        out so it can be paid ahead of demand.
        """
        ready_read, ready_write = os.pipe()
        assignment_read, assignment_write = os.pipe()
        # The lifeline. Nothing is ever sent down it: the child holds the read end for its whole
        # life and stops the moment it reads end-of-file, which happens when the last copy of the
        # write end closes. This process holds that copy, so the child outlives this one by
        # exactly as long as it takes to notice — whether this process exits cleanly, crashes, or
        # is killed outright.
        #
        # That last case is the point. Every other way of ending a worker requires the parent to
        # still be running well enough to send a signal, so a `SIGKILL` or a crash left the whole
        # tree behind: a prototype and its sessions, reparented to init, serving sockets nobody
        # would ever call again. A descriptor needs no cooperation from a dead process, because
        # closing it is the kernel's job rather than the parent's.
        lifeline_read, lifeline_write = os.pipe()
        # `exec` closes everything marked close-on-exec, which is the default for a pipe. These
        # three have to cross it, and their numbers are what the child is told on its command
        # line. Every *other* worker's descriptors stay close-on-exec, which is what stops one
        # child from holding another's lifeline open and making it immortal.
        os.set_inheritable(assignment_read, True)
        os.set_inheritable(ready_write, True)
        os.set_inheritable(lifeline_read, True)

        try:
            pid = os.fork()
        except OSError as error:
            for descriptor in (ready_read, ready_write, assignment_read, assignment_write,
                               lifeline_read, lifeline_write):
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            logger.error("could not fork: %s", error)
            return None

        if pid == 0:
            with contextlib.suppress(OSError):
                os.close(assignment_write)
            # The child must not hold its own lifeline's write end: it would then be keeping
            # itself alive, and would never see the end-of-file no matter who died.
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
        """Bring the pool back up to strength.

        Never called directly from a request. `_want_refill` marks the need and the event loop
        does the work once it has nothing else to answer, so a fork request is replied to before
        any worker is started for the session after it. Starting one is a fork and an exec — a
        few milliseconds, not seconds, because the child does the importing — but a few
        milliseconds spent inside a request is still spent by whoever is waiting.
        """
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
        """How many to keep. Read afresh so a change in configuration takes effect on the next
        refill rather than at the next restart of this process."""
        return active_tuning().amount(Tunable.warm_workers)

    def _want_refill(self) -> None:
        """Ask for a refill on the next turn of the loop, rather than doing one here."""
        self._refill_wanted = True
        self._interrupt_wait()

    def _fork_session(self, fork: str, assignment: dict) -> None:
        """Give one worker a session, and tell the daemon which process became it.

        `fork` names *this* child and is echoed on every report about it. The session id names
        the conversation, which outlives any one process — so it cannot tell the daemon which
        incarnation a report describes, and a report is useless to a waiter that cannot.

        A parked worker is used when one is available, which is the common case and the fast
        one — it has already imported, so it goes from assignment to serving in milliseconds.
        Otherwise a worker is started here and the session waits for the import, which is what
        every session used to do.
        """
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

        # An assignment is well under the pipe buffer; a write that would block here is a bug in
        # what is being assigned, not in the size. Closing is what ends the child's read.
        try:
            os.write(worker.assignment_write, json.dumps(assignment).encode())
            os.close(worker.assignment_write)
        except OSError as error:
            with contextlib.suppress(OSError):
                os.close(worker.assignment_write)
            with contextlib.suppress(OSError):
                os.close(worker.ready_read)
            logger.error("could not write the assignment for %s: %s", session_id, error)
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
            command = session_command(assignment_read, ready_write, lifeline_read)
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
        """Drop the parent's sockets and pipes. Runs only in the child.

        The `exec` that follows closes all of these anyway — they are close-on-exec, which is
        the default — so this covers the window before it and states the rule outright. The
        rule matters most for the other workers' lifelines: a child holding a sibling's write
        end would keep that sibling alive after everything else had gone, which is the exact
        failure this whole mechanism exists to make impossible."""
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
        # The exit report follows on its own; this is what stops the daemon waiting for it.
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
            # A worker that died while parked belonged to no session, so there is nothing to
            # report — only a gap in the pool to close. Reporting it as an exit would tell the
            # daemon a session ended that was never started.
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
            # The dead child's end of the lifeline is already gone; this drops ours, so the
            # descriptor is not held for the life of the prototype by a process that ended
            # minutes ago.
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
