"""Fork viability probe — macOS only. Run this on the target machine.

`documentation/plans/what-must-be-resident.md` proposes a **prototype**: a resident process
that imports the runtime once, freezes its heap, and forks a session worker per request. The
design rests on one invariant — the prototype is genuinely single-threaded when it forks — and
on three macOS questions Linux cannot answer: whether a child may initialise CoreFoundation
after the fork, whether the TCC Accessibility grant follows a fork, and whether `sandbox-exec`
still works from a forked child.

All three have been answered, and this probe exists to re-establish them on any machine and to
catch the invariant breaking again, because the first version of this file could not.

Two lessons are built into the checks below, both learned the hard way.

`threading.enumerate` cannot see native threads. The first version used it, reported one
thread, and passed a parent that actually had three: `src/daisy/base/models.py` runs a blocking
`httpx.get` at import time, and on macOS that fetch spawns two persistent native network
threads. The child then died with `+[__NSPlaceholderSet initialize] may have been in progress
in another thread when fork() was called` — the *multi-threaded-fork* Objective-C abort, which
reads like the CoreFoundation verdict and is not it. So the thread count here comes from mach
`task_threads`, which counts what `fork(2)` actually cares about.

A `sys.modules` census cannot see linked dynamic libraries. CoreFoundation, Foundation,
CoreGraphics and SystemConfiguration are in the image before any Daisy import — they ship
linked into the interpreter. A census that looks for them by module name therefore always
reports none and provides no safety at all. Linkage turns out to be harmless, because only
initialisation matters, so the loaded-image listing below is informational and the blocking
check is on the PyObjC bridge modules, which is what `daisy.computer` would drag in.

Output is structured: every record carries a static message and its data as named fields, so a
run can be read by a person and parsed by a machine without either guessing.

    PYTHONPATH=src uv run python scripts/probe_fork_macos.py

Exit status is 0 when every check passes, 1 on failure, 2 when not run on macOS.
"""

from __future__ import annotations

import ctypes
import gc
import importlib.abc
import json
import logging
import os
import subprocess
import sys
import time
import traceback
from typing import Any, Callable

logger = logging.getLogger("daisy.probe.fork")

# The PyObjC bridge modules. Their presence means something imported `daisy.computer`, or a
# dependency reached for a framework, at module level — which initialises the Objective-C
# runtime in the prototype and makes forking it undefined behaviour.
PYOBJC_BRIDGE_MODULES = frozenset({
    "objc", "Foundation", "AppKit", "Quartz", "CoreFoundation", "ApplicationServices",
    "CoreServices", "CoreGraphics", "WebKit", "Cocoa", "SystemConfiguration",
})

# Frameworks worth naming in the informational loaded-image listing.
FRAMEWORK_NAMES = (
    "CoreFoundation", "Foundation", "CoreGraphics", "AppKit", "ApplicationServices",
    "SystemConfiguration", "Security", "CFNetwork",
)

# Attributes the logging module puts on every record. Anything else was passed by a caller
# through `extra` and is therefore this probe's own structured data.
_STANDARD_RECORD_ATTRIBUTES = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName", "levelname",
    "levelno", "lineno", "message", "module", "msecs", "msg", "name", "pathname", "process",
    "processName", "relativeCreated", "stack_info", "stack_level", "taskName", "thread",
    "threadName",
})

FLEET_CHILDREN = 4
CHILD_REPORT_TIMEOUT_SECONDS = 90.0
CHILD_IDLE_SECONDS = 120


class StructuredFormatter(logging.Formatter):
    """Renders a record as a static message followed by its named fields.

    The message never carries interpolated data. Everything measured travels in `extra`, so a
    line stays greppable by message and parseable by field, and adding a field never rewrites
    the sentence a reader has learned to recognise.
    """

    def format(self, record: logging.LogRecord) -> str:
        fields = {
            name: value for name, value in record.__dict__.items()
            if name not in _STANDARD_RECORD_ATTRIBUTES and not name.startswith("_")
        }
        rendered = " ".join(
            f"{name}={self._render(value)}" for name, value in fields.items()
        )
        head = f"{record.levelname:<7} {record.getMessage()}"
        return f"{head}  {rendered}" if rendered else head

    @staticmethod
    def _render(value: Any) -> str:
        if isinstance(value, (list, tuple)):
            value = ",".join(str(item) for item in value) or "none"
        elif isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, float):
            value = f"{value:.2f}"
        text = str(value)
        return f'"{text}"' if (" " in text or not text) else text


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def log_event(level: int, message: str, **fields: Any) -> None:
    """Log a static message with its data as named fields.

    Field names are checked against the attributes `logging` puts on every record, because a
    collision there raises rather than being ignored — `module` is the obvious trap, and a
    diagnostic that crashes while reporting a finding is worse than no diagnostic. A colliding
    name is suffixed rather than dropped, so nothing measured is silently lost.
    """
    safe_fields = {
        (f"{name}_value" if name in _STANDARD_RECORD_ATTRIBUTES else name): value
        for name, value in fields.items()
    }
    logger.log(level, message, extra=safe_fields)


def log_info(message: str, **fields: Any) -> None:
    log_event(logging.INFO, message, **fields)


def log_warning(message: str, **fields: Any) -> None:
    log_event(logging.WARNING, message, **fields)


def log_error(message: str, **fields: Any) -> None:
    log_event(logging.ERROR, message, **fields)


class MachInterface:
    """Bound on first use rather than at import.

    These symbols exist only on Darwin, and binding them at module level would make the probe
    fail its own platform guard before it could report it.
    """

    def __init__(self) -> None:
        self._library = ctypes.CDLL(None)
        self._task_port = ctypes.c_uint.in_dll(self._library, "mach_task_self_").value
        self._library.task_threads.argtypes = [
            ctypes.c_uint,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_uint)),
            ctypes.POINTER(ctypes.c_uint),
        ]
        self._library.mach_port_deallocate.argtypes = [ctypes.c_uint, ctypes.c_uint]
        self._library.vm_deallocate.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t]
        self._library._dyld_image_count.restype = ctypes.c_uint32
        self._library._dyld_get_image_name.restype = ctypes.c_char_p
        self._library._dyld_get_image_name.argtypes = [ctypes.c_uint32]

    def native_thread_count(self) -> int:
        """Native threads in this task. This is the number `fork(2)` cares about, and it is
        the number `threading.enumerate` cannot see.

        Every port the kernel hands back is deallocated, and so is the array holding them: a
        probe that leaked a mach port per call would change the thing it is measuring.
        """
        thread_ports = ctypes.POINTER(ctypes.c_uint)()
        port_count = ctypes.c_uint(0)
        result = self._library.task_threads(
            self._task_port, ctypes.byref(thread_ports), ctypes.byref(port_count)
        )
        if result != 0:
            return -1
        total = port_count.value
        for index in range(total):
            self._library.mach_port_deallocate(self._task_port, thread_ports[index])
        self._library.vm_deallocate(
            self._task_port,
            ctypes.cast(thread_ports, ctypes.c_void_p),
            total * ctypes.sizeof(ctypes.c_uint),
        )
        return total

    def loaded_frameworks(self) -> list[str]:
        """Frameworks present as loaded mach-O images.

        Informational only. Being linked is expected and harmless — it is precisely why a
        `sys.modules` census can never answer this question.
        """
        found: set[str] = set()
        for index in range(self._library._dyld_image_count()):
            raw_name = self._library._dyld_get_image_name(index) or b""
            image_path = raw_name.decode(errors="replace")
            for framework in FRAMEWORK_NAMES:
                if f"/{framework}.framework/" in image_path:
                    found.add(framework)
        return sorted(found)


class ThreadAttributingFinder(importlib.abc.MetaPathFinder):
    """Wraps every module loader so an import that changes the native thread count is named.

    Deltas nest, because a parent's delta includes its children's, so the deepest recorded
    entry is the true culprit and the shallower ones are the importers that pulled it in.
    """

    def __init__(self, mach: MachInterface) -> None:
        self._mach = mach
        self._depth = 0
        self.observations: list[dict[str, Any]] = []

    def find_spec(self, module_name, search_path=None, target=None):
        for finder in sys.meta_path:
            if finder is self:
                continue
            spec = finder.find_spec(module_name, search_path, target)
            if spec is not None and spec.loader is not None:
                spec.loader = _AttributingLoader(spec.loader, module_name, self)
                return spec
        return None

    def record(self, module_name: str, before: int, after: int) -> None:
        self.observations.append({
            "depth": self._depth,
            "module_name": module_name,
            "threads_before": before,
            "threads_after": after,
        })

    def enter(self) -> None:
        self._depth += 1

    def leave(self) -> None:
        self._depth -= 1

    def count(self) -> int:
        return self._mach.native_thread_count()


class _AttributingLoader(importlib.abc.Loader):
    def __init__(self, inner_loader, module_name: str, finder: ThreadAttributingFinder) -> None:
        self._inner_loader = inner_loader
        self._module_name = module_name
        self._finder = finder

    def create_module(self, spec):
        return self._inner_loader.create_module(spec)

    def exec_module(self, module) -> None:
        threads_before = self._finder.count()
        self._finder.enter()
        try:
            self._inner_loader.exec_module(module)
        finally:
            self._finder.leave()
            threads_after = self._finder.count()
            if threads_after != threads_before:
                self._finder.record(self._module_name, threads_before, threads_after)

    def __getattr__(self, attribute_name):
        return getattr(self._inner_loader, attribute_name)


def resident_memory_mb(process_id: int | None = None) -> float:
    target = os.getpid() if process_id is None else process_id
    completed = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(target)], capture_output=True, text=True
    )
    reported = completed.stdout.strip()
    return (int(reported) / 1024) if reported.isdigit() else 0.0


def physical_footprint(process_id: int) -> str:
    """macOS `footprint` reports phys_footprint, which accounts shared pages honestly.

    It is the closest equivalent to Linux proportional set size, and the reason resident size
    must not be used here: resident size double-counts every page a forked child shares with
    its parent.
    """
    try:
        completed = subprocess.run(
            ["/usr/bin/footprint", "-p", str(process_id)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    for line in completed.stdout.splitlines():
        if "phys_footprint" in line or line.strip().startswith("TOTAL"):
            return line.strip()
    return "unavailable"


def run_in_forked_child(
    check_name: str,
    body: Callable[[], dict[str, Any]],
    timeout_seconds: float = CHILD_REPORT_TIMEOUT_SECONDS,
) -> bool:
    """Fork, run `body` in the child, and log what it reported.

    The child returns a mapping, which travels back as JSON so the parent can log it as named
    fields rather than reformatting a sentence. A child that produces nothing has almost
    certainly aborted, and the terminating signal is itself the answer, so the timeout is a
    result rather than an error.
    """
    read_descriptor, write_descriptor = os.pipe()
    child_process_id = os.fork()
    if child_process_id == 0:
        os.close(read_descriptor)
        try:
            payload = {"outcome": "pass", **body()}
        except BaseException as error:  # noqa: BLE001 — the point is to catch anything at all
            payload = {
                "outcome": "error",
                "error_type": type(error).__name__,
                "error": str(error),
                "traceback": traceback.format_exc()[:1200],
            }
        try:
            os.write(write_descriptor, json.dumps(payload).encode())
        finally:
            os.close(write_descriptor)
            os._exit(0)

    os.close(write_descriptor)
    os.set_blocking(read_descriptor, False)
    deadline = time.monotonic() + timeout_seconds
    received = b""
    while time.monotonic() < deadline:
        try:
            chunk = os.read(read_descriptor, 8192)
        except BlockingIOError:
            time.sleep(0.05)
            continue
        if not chunk:
            break
        received += chunk
    os.close(read_descriptor)

    terminating_signal = 0
    try:
        _, wait_status = os.waitpid(child_process_id, os.WNOHANG)
        if os.WIFSIGNALED(wait_status):
            terminating_signal = os.WTERMSIG(wait_status)
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(child_process_id, 9)
        os.waitpid(child_process_id, 0)
    except (ProcessLookupError, ChildProcessError):
        pass

    if not received:
        log_error(
            "check produced no output; the child almost certainly aborted",
            check=check_name,
            outcome="no_output",
            terminating_signal=terminating_signal,
            aborted=terminating_signal == 6,
            timeout_seconds=timeout_seconds,
        )
        return False

    try:
        report = json.loads(received.decode())
    except ValueError:
        log_error(
            "check returned something that was not JSON",
            check=check_name,
            outcome="malformed",
            raw=received[:400].decode(errors="replace"),
        )
        return False

    passed = report.get("outcome") == "pass"
    log_event(
        logging.INFO if passed else logging.ERROR,
        "check finished",
        check=check_name,
        **report,
    )
    return passed


def check_builds_runtime() -> dict[str, Any]:
    """A forked child must be able to build the thing a real assignment builds."""
    os.setsid()
    from daisy.base.configuration import (
        GlobalConfiguration,
        list_agents,
        load_agent_configuration,
    )
    from daisy.runtime.runtime import AgentRuntime

    configuration = GlobalConfiguration.load()
    agent_names = [entry["id"] for entry in list_agents(configuration.agent_directories())]
    if not agent_names:
        raise RuntimeError("no agent profiles found; seed ~/.agents first")
    runtime = AgentRuntime(
        agent_configuration=load_agent_configuration(
            agent_names[0], configuration.agent_directories()
        ),
        global_configuration=configuration,
        session_id="probe",
        working_directory=os.getcwd(),
    )
    return {
        "agent": agent_names[0],
        "tools": len(runtime._tools),
        "is_session_leader": os.getpid() == os.getsid(0),
        "resident_mb": round(resident_memory_mb(), 1),
    }


def check_coreframework_after_fork() -> dict[str, Any]:
    """The decisive one: CoreFoundation initialised for the first time after the fork.

    If macOS refuses this, the prototype design cannot work and Part II of the plan is dead.
    """
    import ApplicationServices

    is_trusted = ApplicationServices.AXIsProcessTrusted()
    import Quartz

    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    )
    return {
        "coreframework_initialised": True,
        "accessibility_trusted": bool(is_trusted),
        "windows_visible": len(windows or []),
    }


def check_accessibility_grant() -> dict[str, Any]:
    """Whether the TCC grant follows a fork. Compared against the parent's own answer."""
    import ApplicationServices

    return {"accessibility_trusted": bool(ApplicationServices.AXIsProcessTrusted())}


def check_confinement() -> dict[str, Any]:
    """Confinement must survive a fork. It should, because it is applied at an exec."""
    from daisy.base import confinement

    profile = confinement.Profile()
    recipe = confinement.spawn_recipe(profile, workspace=os.getcwd())
    completed = subprocess.run(
        confinement.resolve_command("echo sandboxed-ok", recipe),
        capture_output=True, text=True, timeout=20,
        preexec_fn=recipe.preexec, cwd=os.getcwd(),
    )
    return {
        "backend": confinement.backend_name() or "none",
        "return_code": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip()[:200],
    }


def report_prototype_state(mach: MachInterface, finder: ThreadAttributingFinder,
                           import_seconds: float) -> list[str]:
    """Describe the parked prototype, and return the blockers found in it."""
    import threading

    native_threads = mach.native_thread_count()
    bridges = sorted({
        name for name in sys.modules if name.split(".")[0] in PYOBJC_BRIDGE_MODULES
    })
    log_info(
        "prototype parked",
        resident_mb=round(resident_memory_mb(), 1),
        import_seconds=round(import_seconds, 2),
        module_count=len(sys.modules),
        frozen_objects=gc.get_freeze_count(),
    )
    log_info(
        "thread census",
        native_threads=native_threads,
        python_threads=threading.active_count(),
        note="native is what fork cares about; python cannot see native threads",
    )
    log_info(
        "framework images linked",
        frameworks=mach.loaded_frameworks(),
        note="informational; linkage is expected and only initialisation matters",
    )
    log_info("pyobjc bridge modules imported", bridge_modules=bridges or "none")

    for observation in sorted(finder.observations, key=lambda entry: -entry["depth"]):
        log_warning(
            "import changed the native thread count",
            note="deepest entry is the culprit",
            **observation,
        )

    blockers: list[str] = []
    if native_threads > 1:
        log_error(
            "prototype is multi-threaded and cannot be forked safely",
            native_threads=native_threads,
            consequence="the child inherits locks those threads hold and aborts on its first "
                        "Objective-C call",
            remedy="make the import named above lazy",
        )
        blockers.append("multi_threaded_prototype")
    if bridges:
        log_error(
            "prototype has initialised the Objective-C runtime and cannot be forked safely",
            bridge_modules=bridges,
            remedy="make the module-level import lazy",
        )
        blockers.append("objc_runtime_initialised")
    return blockers


def report_fleet_cost() -> None:
    """What a fleet actually costs on this machine, in the units that tell the truth."""
    children: list[int] = []
    for _ in range(FLEET_CHILDREN):
        read_descriptor, write_descriptor = os.pipe()
        child_process_id = os.fork()
        if child_process_id == 0:
            os.close(read_descriptor)
            try:
                from daisy.base.configuration import GlobalConfiguration

                GlobalConfiguration.load()
                os.write(write_descriptor, b"ready")
            finally:
                os.close(write_descriptor)
            time.sleep(CHILD_IDLE_SECONDS)
            os._exit(0)
        os.close(write_descriptor)
        os.read(read_descriptor, 8)
        os.close(read_descriptor)
        children.append(child_process_id)

    log_info(
        "fleet cost measured",
        role="prototype",
        process_id=os.getpid(),
        resident_mb=round(resident_memory_mb(), 1),
        footprint=physical_footprint(os.getpid()),
    )
    for child_process_id in children:
        log_info(
            "fleet cost measured",
            role="session",
            process_id=child_process_id,
            resident_mb=round(resident_memory_mb(child_process_id), 1),
            footprint=physical_footprint(child_process_id),
        )
    for child_process_id in children:
        try:
            os.kill(child_process_id, 9)
            os.waitpid(child_process_id, 0)
        except (ProcessLookupError, ChildProcessError):
            pass


def main() -> int:
    configure_logging()
    if sys.platform != "darwin":
        log_warning(
            "probe skipped; it is macOS-only",
            platform=sys.platform,
            note="the Linux results are recorded in "
                 "documentation/plans/what-must-be-resident.md",
        )
        return 2

    mach = MachInterface()
    log_info(
        "probe started",
        python=sys.version.split()[0],
        interpreter_threads=mach.native_thread_count(),
    )

    # Import the runtime under instrumentation, so a thread can be attributed to the module
    # that started it rather than merely counted.
    finder = ThreadAttributingFinder(mach)
    sys.meta_path.insert(0, finder)
    started_at = time.monotonic()
    import daisy.worker.session  # noqa: F401
    import_seconds = time.monotonic() - started_at
    sys.meta_path.remove(finder)
    gc.collect()
    gc.freeze()

    failures = report_prototype_state(mach, finder, import_seconds)

    if not run_in_forked_child("builds_runtime", check_builds_runtime):
        failures.append("builds_runtime")
    if not run_in_forked_child("coreframework_after_fork", check_coreframework_after_fork):
        failures.append("coreframework_after_fork")
    if not run_in_forked_child("accessibility_grant", check_accessibility_grant):
        failures.append("accessibility_grant")

    # The parent touches CoreFoundation last and deliberately, so that every check above ran
    # against a parent that had never initialised it.
    try:
        import ApplicationServices

        log_info(
            "parent accessibility grant",
            accessibility_trusted=bool(ApplicationServices.AXIsProcessTrusted()),
            note="a mismatch with the child means TCC does not follow a fork",
        )
    except Exception as error:  # noqa: BLE001 — a probe must not fail on its own reporting
        log_warning("parent could not query accessibility", error=str(error))

    if not run_in_forked_child("confinement", check_confinement):
        failures.append("confinement")

    report_fleet_cost()

    if failures:
        log_error(
            "probe failed",
            failures=failures,
            guidance="a multi-threaded parent confounds the CoreFoundation and accessibility "
                     "checks; fix that first, because the Objective-C multi-threaded-fork "
                     "abort looks exactly like the CoreFoundation verdict and is not it",
        )
        return 1

    log_info(
        "probe passed; the prototype design is viable on this machine",
        resident_mb=round(resident_memory_mb(), 1),
        import_seconds=round(import_seconds, 2),
        native_threads=mach.native_thread_count(),
        frozen_objects=gc.get_freeze_count(),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
