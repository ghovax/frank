"""Fork viability probe — macOS only. Run this on the target machine.

`documentation/plans/what-must-be-resident.md` proposes a **prototype**: a resident process
that imports the runtime once, freezes its heap, and forks a session worker per request. The
design rests on one invariant — **the prototype is genuinely single-threaded when it forks** —
and on three macOS questions Linux cannot answer: whether a child may initialise CoreFoundation
after the fork, whether the TCC Accessibility grant follows a fork, and whether `sandbox-exec`
still works from a forked child.

All three now have answers, and they are the ones the plan hoped for. This probe exists to
re-establish them on any machine, and — more importantly — to catch the invariant breaking
again, because the first version of this file could not.

**Two lessons are built into the checks below, both learned the hard way.**

`threading.enumerate()` cannot see native threads. The first version used it, reported "1
thread", and passed a parent that actually had three: `src/daisy/base/models.py` ran a blocking
`httpx.get` at import time, and on macOS that fetch spawns two persistent native network
threads. The child then died with `+[__NSPlaceholderSet initialize] may have been in progress
in another thread when fork() was called` — the *multi-threaded-fork* ObjC abort, which reads
like the CoreFoundation verdict and is not it. So the thread count here comes from mach
`task_threads`, which counts what `fork(2)` actually cares about.

A `sys.modules` census cannot see linked dylibs. CoreFoundation, Foundation, CoreGraphics and
SystemConfiguration are in the image before any Daisy import — they ship linked into the
interpreter. A census that looks for them by module name therefore always reports "none" and
provides no safety at all. Linkage turns out to be harmless (only *initialisation* matters), so
the dyld census below is informational, and the blocking check is on the PyObjC **bridge**
modules, which is what `daisy.computer` would drag in.

    PYTHONPATH=src uv run python scripts/probe_fork_macos.py

Exit status is 0 when every test passes, 1 on failure, 2 when not run on macOS.
"""

from __future__ import annotations

import ctypes
import gc
import importlib.abc
import json
import os
import subprocess
import sys
import time

FAILURES: list[str] = []

# The PyObjC *bridge* modules. Their presence means something imported `daisy.computer`, or a
# dependency reached for a framework, at module level — which initialises the Objective-C
# runtime in the prototype and makes forking it undefined behaviour.
PYOBJC_BRIDGES = {
    "objc", "Foundation", "AppKit", "Quartz", "CoreFoundation", "ApplicationServices",
    "CoreServices", "CoreGraphics", "WebKit", "Cocoa", "SystemConfiguration",
}

# Frameworks worth naming in the informational dyld listing.
FRAMEWORK_HINTS = (
    "CoreFoundation", "Foundation", "CoreGraphics", "AppKit", "ApplicationServices",
    "SystemConfiguration", "Security", "CFNetwork",
)


# ---------------------------------------------------------------- counting what fork() sees

_mach: tuple = ()


def _mach_bindings() -> tuple:
    """Bound on first use, not at import: these symbols only exist on Darwin, and binding them
    at module level would make the probe fail its own platform guard before it could print it."""
    global _mach
    if _mach:
        return _mach
    libc = ctypes.CDLL(None)
    task = ctypes.c_uint.in_dll(libc, "mach_task_self_").value
    libc.task_threads.argtypes = [
        ctypes.c_uint, ctypes.POINTER(ctypes.POINTER(ctypes.c_uint)), ctypes.POINTER(ctypes.c_uint)
    ]
    libc.mach_port_deallocate.argtypes = [ctypes.c_uint, ctypes.c_uint]
    libc.vm_deallocate.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t]
    libc._dyld_image_count.restype = ctypes.c_uint32
    libc._dyld_get_image_name.restype = ctypes.c_char_p
    libc._dyld_get_image_name.argtypes = [ctypes.c_uint32]
    _mach = (libc, task)
    return _mach


def os_thread_count() -> int:
    """Native threads in this task, via mach. This is the number `fork(2)` cares about;
    `threading.enumerate()` only knows about threads CPython created itself.

    Every port the kernel hands back is deallocated, and so is the array — a probe that leaked
    a mach port per call would change the thing it is measuring."""
    libc, task = _mach_bindings()
    listing = ctypes.POINTER(ctypes.c_uint)()
    count = ctypes.c_uint(0)
    if libc.task_threads(task, ctypes.byref(listing), ctypes.byref(count)) != 0:
        return -1
    total = count.value
    for index in range(total):
        libc.mach_port_deallocate(task, listing[index])
    libc.vm_deallocate(
        task, ctypes.cast(listing, ctypes.c_void_p), total * ctypes.sizeof(ctypes.c_uint)
    )
    return total


def loaded_frameworks() -> list[str]:
    """Frameworks present as loaded mach-O images. Informational: being linked is expected and
    harmless, and is precisely why a `sys.modules` census can never answer this."""
    libc, _ = _mach_bindings()
    found: set[str] = set()
    for index in range(libc._dyld_image_count()):
        name = (libc._dyld_get_image_name(index) or b"").decode(errors="replace")
        for hint in FRAMEWORK_HINTS:
            if f"/{hint}.framework/" in name:
                found.add(hint)
    return sorted(found)


# ------------------------------------------------------- attributing threads to their import

_thread_events: list[tuple[int, str, int, int]] = []
_depth = 0


class _AttributingFinder(importlib.abc.MetaPathFinder):
    """Wraps every loader so a module that changes the native thread count is named.

    Deltas nest — a parent's delta includes its children's — so the *deepest* entry is the true
    culprit and the shallower ones are its importers."""

    def find_spec(self, fullname, path=None, target=None):
        for finder in sys.meta_path:
            if finder is self:
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None and spec.loader is not None:
                spec.loader = _AttributingLoader(spec.loader, fullname)
                return spec
        return None


class _AttributingLoader(importlib.abc.Loader):
    def __init__(self, inner, name: str) -> None:
        self._inner, self._name = inner, name

    def create_module(self, spec):
        return self._inner.create_module(spec)

    def exec_module(self, module) -> None:
        global _depth
        before = os_thread_count()
        _depth += 1
        try:
            self._inner.exec_module(module)
        finally:
            _depth -= 1
            after = os_thread_count()
            if after != before:
                _thread_events.append((_depth, self._name, before, after))

    def __getattr__(self, item):
        return getattr(self._inner, item)


# ----------------------------------------------------------------------------- test harness

def run_child(name: str, body, timeout: float = 90.0) -> bool:
    """Fork, run `body` in the child, report what happened. Times out rather than hanging — an
    ObjC or CoreFoundation abort produces no output, and the signal is the answer."""
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
    signalled = ""
    try:
        _, raw = os.waitpid(pid, os.WNOHANG)
        if os.WIFSIGNALED(raw):
            number = os.WTERMSIG(raw)
            signalled = f" [killed by signal {number}{' — abort, look for an objc[] line above' if number == 6 else ''}]"
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError):
        pass
    text = buffer.decode(errors="replace") or f"NO OUTPUT in {timeout:.0f}s{signalled}"
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
    print(f"interpreter alone: {os_thread_count()} OS thread(s)\n")

    # The prototype's exact state: the whole runtime, frozen, and nothing else — imported under
    # instrumentation so a thread can be attributed to the module that started it.
    sys.meta_path.insert(0, _AttributingFinder())
    started = time.monotonic()
    import daisy.worker.session  # noqa: F401
    import_seconds = time.monotonic() - started
    sys.meta_path.pop(0)
    gc.collect()
    gc.freeze()

    import threading

    native = os_thread_count()
    bridges = sorted({m for m in sys.modules if m.split(".")[0] in PYOBJC_BRIDGES})
    print(f"prototype parked: {rss_mb():.1f} MB   import={import_seconds:.2f}s   "
          f"modules={len(sys.modules)}   frozen={gc.get_freeze_count()}")
    print(f"OS threads (mach task_threads): {native}      <-- the number fork(2) cares about")
    print(f"python threads (threading):     {threading.active_count()}"
          f"      <-- cannot see native threads; never trust this alone")
    print(f"frameworks linked into the image: {', '.join(loaded_frameworks()) or 'none'}")
    print("  (informational — linkage is expected and harmless; only initialisation matters)")
    print(f"PyObjC bridge modules imported: {bridges or 'none'}")

    if _thread_events:
        print("\nmodules whose import changed the native thread count "
              "(deepest entry is the culprit; shallower ones are its importers):")
        for depth, name, before, after in sorted(_thread_events, key=lambda event: -event[0]):
            print(f"    depth={depth:<3} {name:<52} {before} -> {after}")

    if native > 1:
        print(f"\n  ^^ BLOCKER. Forking a parent with {native} native threads inherits any lock\n"
              "     those threads hold, and the child aborts on its first ObjC/CF call. The\n"
              "     attribution above names the import to make lazy.")
        FAILURES.append(f"prototype has {native} native threads")
    if bridges:
        print("\n  ^^ BLOCKER. A prototype that has already initialised the Objective-C runtime\n"
              "     cannot be forked safely. Find the module-level import and make it lazy.")
        FAILURES.append("prototype imported the PyObjC bridge")
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
        print("\nRead the failures in order. A multi-threaded parent confounds tests 2 and 3 —\n"
              "fix that first, because an ObjC multi-threaded-fork abort looks exactly like the\n"
              "CoreFoundation verdict and is not it. Only test 2 failing from a genuinely\n"
              "single-threaded parent means the prototype design is not viable on macOS.")
        return 1
    print("All tests passed — the prototype design is viable on this machine.")
    print(json.dumps({
        "parked_rss_mb": round(rss_mb(), 1),
        "import_seconds": round(import_seconds, 2),
        "os_threads": native,
        "frozen_objects": gc.get_freeze_count(),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
