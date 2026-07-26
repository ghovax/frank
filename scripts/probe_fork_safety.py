#!/usr/bin/env python3
"""Answer, on macOS, whether Daisy can serve sessions from a fork server.

A fork server imports the heavy runtime once and forks a worker per session, so the shared
pages are paid for once. On Linux this is measured and works: a worker costs about two
megabytes of private memory instead of the two hundred and sixty it costs today. macOS is the
open question, and it is open for one specific reason.

Apple's position, stated by DTS in the developer forums, is that `fork()` without `exec()`
works reliably only if you stay within POSIX; the moment Objective-C or any higher-level
framework is involved you are outside what is supported. The concrete mechanism is a check
added in macOS 10.13: the Objective-C runtime registers `pthread_atfork` handlers, notes that
a fork happened without an exec, and then **aborts if the child triggers `+initialize` on a
class that was not already initialised in the parent** — "may have been in progress in another
thread when fork() was called". CPython drew the same conclusion and changed multiprocessing's
default start method on macOS to `spawn` in 3.8.

Note which direction that runs, because it is the opposite of the intuitive one. Loading PyObjC
lazily *in the child* is the failure mode, not the safe path. The safe direction is to have
everything initialised *before* the fork — which is why the usual advice is to preload.

That matters here because Daisy's layering check forbids importing `computer/` at module level
with the comment "a parked worker that has loaded PyObjC is not safe to fork". Under the
mechanism above that comment has it backwards: a parent that has fully initialised PyObjC is
the *safer* one. The invariant is still right for its original purpose — keeping the runtime
out of the daemon — but it is not the fork-safety guarantee it reads as.

The remaining question is empirical, because the abort is conservative: it fires whenever a
class is first initialised after a fork, whether or not another thread was really mid-init. In
a strictly single-threaded fork server no other thread can exist, so the hazard the check
guards against is impossible — but the check does not know that and may abort anyway.

Three cases, and the third is the control:

  1. Fork from a parent holding the heavy stack, and have the child load PyObjC and call the
     Accessibility API. This is the arrangement a naive fork server would use, and on the
     reading above it is **expected to fail**.
  2. The same child then runs an asyncio event loop and binds a socket, because a worker is a
     socket server and inheriting a broken runtime would show up here rather than at import.
  3. Fork from a parent that has *already* used PyObjC — the preloaded arrangement. If case 1
     fails and case 3 passes, preloading is the workable shape. If both fail, macOS cannot host
     a fork server at all and should keep exec'd workers.

Run it on the Mac that matters:

    uv run python scripts/probe_fork_safety.py
"""

from __future__ import annotations

import gc
import json
import os
import platform
import sys
import textwrap


def _child_report(**fields) -> bytes:
    return json.dumps(fields).encode()


def _run_child(work) -> dict:
    """Fork, run `work` in the child, and bring back what it managed to say.

    A crash is the interesting outcome, so the child's death is reported rather than raised:
    an empty pipe with a signal in the exit status is exactly the fork-safety failure."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            payload = work()
        except BaseException as error:  # noqa: BLE001 — the child reports, it does not raise
            payload = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        try:
            os.write(write_fd, _child_report(**payload))
        finally:
            os.close(write_fd)
            os._exit(0)
    os.close(write_fd)
    raw = b""
    while chunk := os.read(read_fd, 4096):
        raw += chunk
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    if not raw:
        signalled = os.WTERMSIG(status) if os.WIFSIGNALED(status) else 0
        return {"ok": False, "error": f"child died before reporting (signal {signalled})"}
    return json.loads(raw.decode())


def case_pyobjc_after_fork() -> dict:
    """The naive arrangement: heavy stack in the parent, PyObjC first touched in the child.

    Expected to FAIL. This is the direction the 10.13 check exists to catch."""
    import litellm  # noqa: F401  — the 137 MB this whole exercise is about

    gc.collect()
    gc.freeze()

    def work() -> dict:
        import asyncio

        from ApplicationServices import AXIsProcessTrusted

        trusted = bool(AXIsProcessTrusted())

        async def serve() -> str:
            server = await asyncio.start_unix_server(
                lambda reader, writer: None, path=f"/tmp/daisy-forkprobe-{os.getpid()}.sock"
            )
            name = server.sockets[0].getsockname()
            server.close()
            await server.wait_closed()
            os.unlink(name)
            return name

        socket_name = asyncio.run(serve())
        return {"ok": True, "trusted": trusted, "socket": os.path.basename(socket_name)}

    return _run_child(work)


def case_pyobjc_before_fork() -> dict:
    """The preloaded arrangement: the parent initialises PyObjC, then forks.

    Expected to PASS. If it does, a fork server on macOS must preload the frameworks rather
    than avoid them — the opposite of what the layering comment implies."""
    from ApplicationServices import AXIsProcessTrusted

    AXIsProcessTrusted()  # initialise the frameworks in the *parent*

    def work() -> dict:
        from ApplicationServices import AXIsProcessTrusted as check

        return {"ok": True, "trusted": bool(check())}

    return _run_child(work)


def main() -> int:
    if platform.system() != "Darwin":
        print(f"This probe answers a macOS question; you are on {platform.system()}.")
        print("On Linux the same arrangement is already measured and works.")
        return 2

    print(f"macOS {platform.mac_ver()[0]} on {platform.machine()}, python {sys.version.split()[0]}\n")

    print("1. PyObjC first touched in the CHILD after fork (expected FAIL)")
    first = case_pyobjc_after_fork()
    print(f"   {'PASS' if first.get('ok') else 'FAIL'}  {json.dumps(first)}\n")

    print("2. PyObjC preloaded in the PARENT before forking (expected PASS)")
    second = case_pyobjc_before_fork()
    print(f"   {'PASS' if second.get('ok') else 'FAIL'}  {json.dumps(second)}\n")

    lazy_ok, preload_ok = first.get("ok") is True, second.get("ok") is True
    print(textwrap.dedent(f"""
        VERDICT
          lazy PyObjC in the child works : {lazy_ok}
          preloaded PyObjC works         : {preload_ok}
    """).strip())
    if preload_ok:
        print("\n  A macOS fork server is possible, but it must PRELOAD the frameworks in the\n"
              "  parent. Avoiding them is the failing direction.")
    elif lazy_ok:
        print("\n  Unexpected: the documented hazard did not reproduce here. Treat as\n"
              "  inconclusive rather than as permission to ship a fork server.")
    else:
        print("\n  Neither arrangement works. macOS should keep exec'd workers; the fork\n"
              "  server is a Linux-only optimisation.")
    return 0 if (lazy_ok or preload_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
