#!/usr/bin/env python3
"""Answer, on macOS, whether Daisy can serve sessions from a fork server.

A fork server imports the heavy runtime once and forks a worker per session, so the shared
pages are paid for once. On Linux this is measured and works: a worker costs about two
megabytes of private memory instead of the two hundred and sixty it costs today. macOS is the
open question, and it is open for one specific reason.

Apple's position is that `fork()` without a following `exec()` is unsupported once a process
has initialised most frameworks. The failure is not subtle when it happens — the child dies
with "may have been in progress in another thread when fork() was called" — but whether it
happens depends entirely on what the parent touched before forking. Daisy's layering check
already forbids importing `computer/` at module level, with that exact reason written next to
it, so the intended arrangement is that the fork server never loads PyObjC and the child loads
it afterwards. That is the supported direction. This measures whether it is actually true.

Three cases, and the third is the control:

  1. Fork from a parent holding the heavy stack, and have the child load PyObjC and call the
     Accessibility API. This is the arrangement Daisy would use. It must pass.
  2. The same child then runs an asyncio event loop and binds a socket, because a worker is a
     socket server and inheriting a broken runtime would show up here rather than at import.
  3. Fork from a parent that has *already* used PyObjC. This is expected to fail, and it is
     included so a pass in case 1 means something: if case 3 also passes, this machine is not
     exercising the hazard and case 1 proves less than it appears to.

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


def case_worker_after_fork() -> dict:
    """The arrangement Daisy would use: heavy stack in the parent, PyObjC only in the child."""
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
    """The control. Expected to fail; a pass means this machine is not exercising the hazard."""
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

    print("1. worker forked from a heavy parent, PyObjC loaded in the child")
    first = case_worker_after_fork()
    print(f"   {'PASS' if first.get('ok') else 'FAIL'}  {json.dumps(first)}\n")

    print("2. control: parent used PyObjC before forking (expected to FAIL)")
    second = case_pyobjc_before_fork()
    print(f"   {'PASS' if second.get('ok') else 'FAIL'}  {json.dumps(second)}\n")

    verdict = textwrap.dedent(f"""
        VERDICT
          fork server viable on this machine : {first.get('ok') is True}
          hazard actually exercised here     : {second.get('ok') is not True}
    """).strip()
    print(verdict)
    if first.get("ok") and second.get("ok"):
        print(
            "\n  Both passed, which is weaker than it looks: this machine did not reproduce the\n"
            "  documented hazard, so case 1 passing is not evidence that avoiding PyObjC in the\n"
            "  parent is what made it work. Treat as inconclusive."
        )
    return 0 if first.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
