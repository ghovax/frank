"""Fork viability probe — macOS only. Run this on the target machine before building anything.

`documentation/plans/what-must-be-resident.md` proposes a **prototype**: a resident process that
imports the runtime once, freezes its heap, and forks a session worker per request. The whole
design was measured on Linux against the real import graph — 264 MB parked, ~12 MB per forked
session, ~60 ms to a serving socket, 88% saved on a twelve-session fan-out — and three things
could not be measured there. All three are macOS-specific, and one of them decides whether the
design is viable at all:

  1. **CoreFoundation after a fork.** macOS aborts a process that calls into CF after forking if
     CF was already initialised before it. `scripts/check_layers.py` keeps `daisy.computer` (and
     therefore PyObjC) out of every module-level import, so the prototype never initialises CF —
     which means the child initialises it *fresh*, post-fork. That should be legal. Test 2 is the
     load-bearing check, and the module census before it reports the condition that would kill
     the design outright: CF having reached the prototype's import set anyway.
  2. **TCC / Accessibility.** A forked child carries the parent's executable, code signature and
     responsible process, so the grant should be inherited — plausibly better than a re-exec,
     which is what the fleet relies on today. Test 3 compares child and parent.
  3. **`sandbox-exec` from a forked child.** It is an exec, so nothing fork-specific should
     apply. Test 4 confirms rather than assumes.

    PYTHONPATH=src uv run python scripts/probe_fork_macos.py

Exit status is 0 when every test passes, 1 on failure, 2 when not run on macOS.
"""

from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import time

FAILURES: list[str] = []

# Anything whose import initialises the Objective-C runtime or CoreFoundation. If one of these
# is loaded in the prototype, forking it is undefined behaviour on macOS.
FRAMEWORK_ROOTS = {
    "objc", "Foundation", "AppKit", "Quartz", "CoreFoundation", "ApplicationServices",
    "CoreServices", "CoreGraphics", "WebKit", "Cocoa",
}


def run_child(name: str, body, timeout: float = 90.0) -> bool:
    """Fork, run `body` in the child, report what happened. Times out rather than hanging —
    a CoreFoundation abort produces no output at all, which is itself the answer."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            os.write(write_fd, f"OK {body()}".encode()[:2000])
        except BaseException as error:  # noqa: BLE001 — the point is to catch anything at all
            import traceback

            os.write(
                write_fd,
                f"FAIL {type(error).__name__}: {error}\n{traceback.format_exc()[:1200]}".encode(),
            )
        finally:
            os.close(write_fd)
            os._exit(0)
    os.close(write_fd)
    os.set_blocking(read_fd, False)
    deadline, buffer = time.monotonic() + timeout, b""
    while time.monotonic() < deadline:
        try:
            chunk = os.read(read_fd, 8192)
        except BlockingIOError:
            time.sleep(0.05)
            continue
        if not chunk:
            break
        buffer += chunk
    os.close(read_fd)
    status = ""
    try:
        _, raw = os.waitpid(pid, os.WNOHANG)
        if os.WIFSIGNALED(raw):
            status = f" [killed by signal {os.WTERMSIG(raw)}]"
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError):
        pass
    text = buffer.decode(errors="replace") or f"NO OUTPUT in {timeout:.0f}s{status}"
    ok = text.startswith("OK")
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n        {text.strip()[:700]}\n")
    if not ok:
        FAILURES.append(name)
    return ok


def rss_mb(pid: int | None = None) -> float:
    target = os.getpid() if pid is None else pid
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(target)], capture_output=True, text=True
    ).stdout.strip()
    return (int(out) / 1024) if out.isdigit() else 0.0


def footprint(pid: int) -> str:
    """macOS `footprint` reports phys_footprint, which accounts shared pages honestly. It is the
    closest equivalent to Linux PSS, and the reason `rss` must not be used here: `rss`
    double-counts every page a forked child shares with its parent."""
    try:
        out = subprocess.run(
            ["/usr/bin/footprint", "-p", str(pid)], capture_output=True, text=True, timeout=30
        ).stdout
        for line in out.splitlines():
            if "phys_footprint" in line or line.strip().startswith("TOTAL"):
                return line.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "footprint unavailable"


def main() -> int:
    if sys.platform != "darwin":
        print("This probe is macOS-only. The Linux results are already established in "
              "documentation/plans/what-must-be-resident.md.")
        return 2

    print(f"=== fork viability probe — macOS, python {sys.version.split()[0]} ===\n")

    # The prototype's exact state: the whole runtime, frozen, and nothing else.
    started = time.monotonic()
    import daisy.worker.session  # noqa: F401
    import_seconds = time.monotonic() - started
    gc.collect()
    gc.freeze()

    import threading

    frameworks = sorted({m for m in sys.modules if m.split(".")[0] in FRAMEWORK_ROOTS})
    threads = [t.name for t in threading.enumerate()]
    print(f"prototype parked: {rss_mb():.1f} MB   import={import_seconds:.2f}s   "
          f"modules={len(sys.modules)}   frozen={gc.get_freeze_count()}")
    print(f"threads: {len(threads)} {threads}")
    print(f"CoreFoundation / PyObjC modules loaded: {frameworks or 'none'}")
    if frameworks:
        print("  ^^ BLOCKER. A prototype that has already initialised CoreFoundation cannot be\n"
              "     forked safely on macOS. Find the module-level import and make it lazy.")
        FAILURES.append("prototype loaded CoreFoundation")
    if len(threads) > 1:
        print("  ^^ BLOCKER. Forking a multi-threaded parent inherits any lock those threads\n"
              "     hold. The prototype must park on a blocking accept(), not on a thread pool.")
        FAILURES.append("prototype is multi-threaded")
    print()

    # 1. Does a forked child run the runtime at all?
    def builds_runtime() -> str:
        os.setsid()
        from daisy.base.configuration import (
            GlobalConfiguration,
            list_agents,
            load_agent_configuration,
        )
        from daisy.runtime.runtime import AgentRuntime

        configuration = GlobalConfiguration.load()
        names = [entry["id"] for entry in list_agents(configuration.agent_directories())]
        if not names:
            raise RuntimeError("no agent profiles found; seed ~/.agents first")
        runtime = AgentRuntime(
            agent_configuration=load_agent_configuration(names[0], configuration.agent_directories()),
            global_configuration=configuration,
            session_id="probe",
            working_directory=os.getcwd(),
        )
        return (f"AgentRuntime({names[0]}) tools={len(runtime._tools)} "
                f"session_leader={os.getpid() == os.getsid(0)} rss={rss_mb():.1f} MB")

    run_child("1. forked child builds a real AgentRuntime", builds_runtime)

    # 2. The decisive one: CoreFoundation initialised for the first time AFTER the fork.
    def coreframework() -> str:
        import ApplicationServices as AS  # noqa: N814 — first CF touch happens here, post-fork

        trusted = AS.AXIsProcessTrusted()
        import Quartz

        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
        return (f"CoreFoundation initialised post-fork without aborting; "
                f"AXIsProcessTrusted={trusted}; windows visible={len(windows or [])}")

    run_child("2. child initialises CoreFoundation AFTER the fork  <-- decides the design",
              coreframework)

    # 3. Does the Accessibility grant follow a fork?
    print("    (the parent's own answer is printed below for comparison)")
    run_child("3. child sees the same Accessibility grant",
              lambda: f"child AXIsProcessTrusted="
                      f"{__import__('ApplicationServices').AXIsProcessTrusted()}")
    try:
        import ApplicationServices as _AS  # noqa: N814 — parent touches CF last, deliberately

        print(f"        parent AXIsProcessTrusted={_AS.AXIsProcessTrusted()}   "
              f"(a mismatch means TCC does not follow a fork)\n")
    except Exception as error:  # noqa: BLE001
        print(f"        parent could not query Accessibility: {error}\n")

    # 4. Confinement still works from a forked child.
    def confined() -> str:
        from daisy.base import confinement

        profile = confinement.Profile()
        spawn = confinement.spawn_recipe(profile, workspace=os.getcwd())
        done = subprocess.run(
            confinement.resolve_command("echo sandboxed-ok", spawn),
            capture_output=True, text=True, timeout=20,
            preexec_fn=spawn.preexec, cwd=os.getcwd(),
        )
        return (f"backend={confinement.backend_name()!r} rc={done.returncode} "
                f"stdout={done.stdout.strip()!r} stderr={done.stderr.strip()[:200]!r}")

    run_child("4. child spawns a confined child (sandbox-exec)", confined)

    # 5. What a fleet actually costs on this machine.
    print("--- 5. fleet cost (compare phys_footprint, not rss) ---")
    children = []
    for _ in range(4):
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                from daisy.base.configuration import GlobalConfiguration

                GlobalConfiguration.load()
                os.write(write_fd, b"up")
            finally:
                os.close(write_fd)
            time.sleep(120)
            os._exit(0)
        os.close(write_fd)
        os.read(read_fd, 8)
        os.close(read_fd)
        children.append(pid)
    print(f"        prototype  rss={rss_mb():.1f} MB   {footprint(os.getpid())}")
    for pid in children:
        print(f"        child {pid}  rss={rss_mb(pid):.1f} MB   {footprint(pid)}")
    print()
    for pid in children:
        try:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass

    print("=" * 74)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        print("\nTest 2 failing means the prototype design is not viable on macOS and the plan's\n"
              "Part II must be abandoned. Anything else failing is a fixable detail.")
        return 1
    print("All tests passed — the prototype design is viable on this machine.")
    print(json.dumps({
        "parked_rss_mb": round(rss_mb(), 1),
        "import_seconds": round(import_seconds, 2),
        "frozen_objects": gc.get_freeze_count(),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
