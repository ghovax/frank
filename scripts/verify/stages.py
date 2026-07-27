"""The claims, one function each.

Every stage here corresponds to a specific thing this rewrite asserts, and fails if that thing
is not true. They are ordered so that a failure early explains the failures after it: the
prototype's invariant gates everything about forking, forking gates sessions, and sessions gate
sleep and wake.

A stage answers with an :class:`Outcome` rather than raising, so one failure does not hide the
rest of the battery.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from scripts.verify.harness import (
    PERMISSIVE_CONFINEMENT,
    Outcome,
    SOURCE_ROOT,
    confinement_available,
    daemon,
    discard,
    environment_for,
    make_roots,
    seed_configuration,
)


def _agent_name() -> str:
    return "general-assistant"


def structure() -> Outcome:
    """The layering held, every module imports alone, and the catalogues are consistent.

    Cheapest and first, because everything else in the battery imports the tree it checks.
    """
    root = SOURCE_ROOT.parent
    environment = {**os.environ, "PYTHONPATH": str(SOURCE_ROOT)}
    checks: dict[str, list[str]] = {
        "layers": [sys.executable, "scripts/check_layers.py"],
        "imports": [sys.executable, "scripts/check_imports.py"],
        "translations": [sys.executable, "scripts/check_translations.py"],
    }

    # Lint belongs in the battery because it catches a class the other checks cannot see: a
    # `@staticmethod` that kept its `self` parameter imports fine, type-checks nowhere, and
    # fails only when called — which is how the ChatGPT provider stayed broken across two
    # commits. Ruff is a development tool rather than a runtime dependency, so it is looked for
    # rather than assumed, and its absence is reported instead of failing the stage.
    ruff = shutil.which("ruff")
    uv = shutil.which("uv")
    if ruff:
        checks["lint"] = [ruff, "check", "src", "scripts"]
    elif uv:
        checks["lint"] = [uv, "run", "ruff", "check", "src", "scripts"]

    observations: dict[str, Any] = {}
    if "lint" not in checks:
        observations["lint"] = "ruff is not installed; skipped"
    passed = True
    for name, command in checks.items():
        result = subprocess.run(
            command, capture_output=True, text=True, cwd=str(root), env=environment,
        )
        observations[name] = result.returncode == 0
        if result.returncode != 0:
            passed = False
            observations[f"{name}_output"] = (result.stdout + result.stderr)[-1500:]
    return Outcome("structure", passed, observations=observations)


def prototype() -> Outcome:
    """The prototype is single-threaded, frozen, and forks children that report themselves.

    **This is the stage that gates Part II**, and the one that genuinely needs a real machine:
    the failure it guards against is a macOS abort inside the Objective-C runtime, and the
    thread count that predicts it is invisible to `threading.enumerate()`.

    The fork here is one that must *fail* — an assignment naming no agent — which is
    deliberate. It exercises the ready pipe, the failure report and the exit report without
    needing a model provider, and a child that refuses correctly is the same code path as one
    that starts correctly.
    """
    roots = make_roots()
    environment = environment_for(roots)
    sys.path.insert(0, str(SOURCE_ROOT))
    os.environ.update(roots)
    from daisy.base.paths import prototype_socket_path

    socket_path = prototype_socket_path()

    async def drive() -> Outcome:
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "daisy", "prototype",
            env=environment, start_new_session=True,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        reader = writer = None
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            if process.returncode is not None:
                output = (await process.stdout.read()).decode() if process.stdout else ""
                return Outcome("prototype", False, reason="the prototype exited during startup",
                               observations={"output": output[-2000:]})
            try:
                reader, writer = await asyncio.open_unix_connection(str(socket_path))
                break
            except OSError:
                await asyncio.sleep(0.05)
        if writer is None:
            process.terminate()
            return Outcome("prototype", False, reason="the prototype never accepted a connection")

        startup_seconds = round(time.monotonic() - started, 2)

        async def send(payload: dict) -> None:
            writer.write((json.dumps(payload) + "\n").encode())
            await writer.drain()

        async def receive(count: int) -> list[dict]:
            received = []
            for _ in range(count):
                line = await asyncio.wait_for(reader.readline(), timeout=60)
                if not line:
                    break
                received.append(json.loads(line.decode()))
            return received

        await send({"command": "status"})
        status = (await receive(1))[0]

        fork_started = time.monotonic()
        await send({"command": "fork", "assignment": {
            "session_id": "verify-no-agent",
            "socket": str(Path(roots["XDG_RUNTIME_DIR"]) / "verify.sock"),
        }})
        reports = await receive(2)
        kinds = [entry.get("event") for entry in reports]
        fork_seconds = round(time.monotonic() - fork_started, 3)

        await send({"command": "status"})
        after = (await receive(1))[0]

        await send({"command": "shutdown"})
        try:
            await asyncio.wait_for(process.wait(), timeout=30)
        except asyncio.TimeoutError:
            process.terminate()

        observations = {
            "startup_seconds": startup_seconds,
            "native_threads": status.get("threads"),
            "frozen_objects": status.get("frozen"),
            "fork_seconds": fork_seconds,
            "reports": kinds,
            "threads_after_fork": after.get("threads"),
        }
        passed = (
            status.get("threads") == 1
            and int(status.get("frozen") or 0) > 0
            and "failed" in kinds
            and "exited" in kinds
            and after.get("threads") == 1
        )
        reason = "" if passed else (
            "the prototype is not single-threaded — something imported at module scope started "
            "a thread, most likely a network call"
            if status.get("threads") != 1 else "the fork did not report as expected"
        )
        return Outcome("prototype", passed, reason=reason, observations=observations)

    try:
        return asyncio.run(drive())
    finally:
        discard(roots)


def daemon_boot() -> Outcome:
    """The daemon starts, brings the prototype up, and reports both invariants."""
    with daemon(configuration=PERMISSIVE_CONFINEMENT) as instance:
        status = instance.await_prototype()
        passed = bool(status.get("alive")) and status.get("threads") == 1 and int(status.get("frozen_objects") or 0) > 0
        return Outcome(
            "daemon", passed,
            reason="" if passed else "the daemon came up without a healthy prototype",
            observations={"prototype": status},
        )


def session_lifecycle() -> Outcome:
    """A session is a record: it survives the daemon, comes back asleep, and wakes on a message.

    Four claims in one stage because they are one claim seen at four moments, and splitting
    them would mean paying for four daemons and four prototypes to check it.
    """
    available, detail = confinement_available()
    roots = make_roots()
    seed_configuration(roots, PERMISSIVE_CONFINEMENT)
    workspace = tempfile.mkdtemp(prefix="daisy-verify-work-")
    observations: dict[str, Any] = {"confinement": detail}
    try:
        with daemon(roots=roots) as first:
            first.await_prototype()
            created = first.call("session.create", {
                "agent": _agent_name(), "working_directory": workspace,
            })
            session_id = created["id"]
            session_token = created["token"]
            live = first.call("session.get", {"id": session_id})["session"]
            observations["after_create"] = {
                "lifecycle": live.get("lifecycle"), "activity": live.get("activity"),
                "has_process": bool(live.get("pid")),
            }

        # A second daemon over the same directories: the restart, done the honest way.
        with daemon(roots=roots) as second:
            second.await_prototype()
            restored = second.call("session.get", {"id": session_id})["session"]
            observations["after_restart"] = {
                "lifecycle": restored.get("lifecycle"), "activity": restored.get("activity"),
                "has_process": bool(restored.get("pid")),
            }

            # The creator's token must still work: it is derived from the session id, so a
            # daemon that never saw the session minted can still authorise its holder.
            _, token_error = second.try_call("session.get", {"id": session_id}, token=session_token)
            observations["token_survives"] = token_error is None

            # Reads must not wake it.
            second.call("session.list", {})
            still_asleep = second.call("session.get", {"id": session_id})["session"]
            observations["reads_do_not_wake"] = not still_asleep.get("pid")

            # A message must.
            second.call("session.send", {
                "id": session_id, "parts": [{"kind": "text", "text": "hello"}],
            })
            awake = second.call("session.get", {"id": session_id})["session"]
            observations["after_wake"] = {
                "lifecycle": awake.get("lifecycle"), "activity": awake.get("activity"),
                "has_process": bool(awake.get("pid")),
            }

        passed = (
            observations["after_create"]["has_process"]
            and observations["after_restart"]["lifecycle"] == "live"
            and observations["after_restart"]["activity"] == "asleep"
            and not observations["after_restart"]["has_process"]
            and observations["token_survives"]
            and observations["reads_do_not_wake"]
            and observations["after_wake"]["has_process"]
        )
        return Outcome("session", passed, observations=observations)
    finally:
        discard(roots)
        subprocess.run(["rm", "-rf", workspace], check=False)


def reaping() -> Outcome:
    """Killing a session's process is noticed, and the session is marked failed.

    The claim that matters: the daemon did not fork this process and cannot `waitpid` it, so
    noticing at all depends on the prototype reporting the exit.
    """
    roots = make_roots()
    seed_configuration(roots, PERMISSIVE_CONFINEMENT)
    workspace = tempfile.mkdtemp(prefix="daisy-verify-reap-")
    try:
        with daemon(roots=roots) as instance:
            instance.await_prototype()
            created = instance.call("session.create", {
                "agent": _agent_name(), "working_directory": workspace,
            })
            session_id = created["id"]
            record = instance.call("session.get", {"id": session_id})["session"]
            pid = int(record.get("pid") or 0)
            if not pid:
                return Outcome("reaping", False, reason="the session never reported a process")

            os.kill(pid, signal.SIGKILL)
            deadline = time.monotonic() + 60
            after: dict = {}
            while time.monotonic() < deadline:
                after = instance.call("session.get", {"id": session_id})["session"]
                if after.get("lifecycle") == "ended":
                    break
                time.sleep(0.25)

            observations = {
                "killed_pid": pid,
                "lifecycle": after.get("lifecycle"),
                "outcome": after.get("outcome"),
                "exit_reason": after.get("exit_reason"),
            }
            passed = after.get("lifecycle") == "ended" and after.get("outcome") == "failed"
            return Outcome("reaping", passed, observations=observations)
    finally:
        discard(roots)
        subprocess.run(["rm", "-rf", workspace], check=False)


def prototype_death() -> Outcome:
    """A dead prototype does not touch live sessions, and a new session still starts after it."""
    roots = make_roots()
    seed_configuration(roots, PERMISSIVE_CONFINEMENT)
    workspace = tempfile.mkdtemp(prefix="daisy-verify-proto-")
    try:
        with daemon(roots=roots) as instance:
            status = instance.await_prototype()
            prototype_pid = int(status.get("pid") or 0)
            created = instance.call("session.create", {
                "agent": _agent_name(), "working_directory": workspace,
            })
            session_id = created["id"]

            os.kill(prototype_pid, signal.SIGKILL)
            time.sleep(1.0)

            survivor = instance.call("session.get", {"id": session_id})["session"]
            restarted = instance.await_prototype()
            second = instance.call("session.create", {
                "agent": _agent_name(), "working_directory": workspace,
            })

            observations = {
                "killed_prototype": prototype_pid,
                "session_after": {"lifecycle": survivor.get("lifecycle"), "has_process": bool(survivor.get("pid"))},
                "prototype_restarted": bool(restarted.get("alive")),
                "new_prototype_pid": restarted.get("pid"),
                "new_session_started": bool(second.get("id")),
            }
            passed = (
                survivor.get("lifecycle") == "live"
                and bool(restarted.get("alive"))
                and restarted.get("pid") != prototype_pid
                and bool(second.get("id"))
            )
            return Outcome("prototype-death", passed, observations=observations)
    finally:
        discard(roots)
        subprocess.run(["rm", "-rf", workspace], check=False)


def reentrancy() -> Outcome:
    """Two runtimes in one process keep their own confinement.

    The regression `dispatch.py` used to have: a `bash` call naming its own working directory
    rewrote the *process-global* confinement profile, so a second turn in the same worker
    inherited a sandbox narrowed to a directory it had never heard of.
    """
    sys.path.insert(0, str(SOURCE_ROOT))
    from daisy.base.confinement import Profile
    from daisy.runtime.tools import context as tool_context

    first = Profile()
    second = Profile()
    observations: dict[str, Any] = {}

    token_one = tool_context.bind(tool_context.ToolContext(sandbox=first, workspace="/one"))
    seen_one = tool_context.current().workspace
    # A derived context, which is what a `bash` call somewhere else now produces.
    token_two = tool_context.bind(tool_context.current().for_directory("/two"))
    seen_two = tool_context.current().workspace
    tool_context.unbind(token_two)
    seen_one_again = tool_context.current().workspace
    tool_context.unbind(token_one)
    seen_empty = tool_context.current().workspace

    observations = {
        "bound": seen_one, "narrowed": seen_two,
        "restored": seen_one_again, "unbound": seen_empty,
        "distinct_profiles": first is not second,
    }
    passed = seen_one == "/one" and seen_two == "/two" and seen_one_again == "/one" and seen_empty == ""
    return Outcome("reentrancy", passed, observations=observations)


def library_ports() -> Outcome:
    """Every seam is replaceable, and the library writes nothing to the caller's disk.

    Uses a stub chat model, so it needs no provider and no network. That is the point of the
    model seam, and exercising it here is what proves the seam rather than describing it.
    """
    roots = make_roots()
    os.environ.update(roots)
    sys.path.insert(0, str(SOURCE_ROOT))

    async def drive() -> Outcome:
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage, AIMessageChunk
        from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

        from daisy import Approval, MemoryCheckpoints, Session

        class StubModel(BaseChatModel):
            reply: str = "the stub model answered"

            @property
            def _llm_type(self) -> str:
                return "verify-stub"

            def bind_tools(self, *_args: Any, **_kwargs: Any):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])

            async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
                yield ChatGenerationChunk(message=AIMessageChunk(content=[
                    {"type": "text", "text": self.reply, "id": "block-0", "index": 0}
                ]))

        observations: dict[str, Any] = {}
        seen: list[Any] = []

        class ListObserver:
            def observe(self, observation):
                seen.append(observation)

        class AllowEverything:
            def __init__(self) -> None:
                self.gates: list[str] = []

            async def decide(self, gate):
                self.gates.append(gate.kind)
                return Approval(allow=True, reason="verification")

        class Incomplete:
            async def save(self, session_id, state): ...

        try:
            Session(_agent_name(), checkpoints=Incomplete())
            observations["rejects_incomplete_port"] = ""
        except TypeError as error:
            observations["rejects_incomplete_port"] = str(error)

        checkpoints = MemoryCheckpoints()
        session = Session(
            _agent_name(), directory=".", session_id="verify-session",
            model=StubModel(), checkpoints=checkpoints,
            observer=ListObserver(), approvals=AllowEverything(),
        )
        answer = await session.ask("say something")
        saved = await checkpoints.load("verify-session")

        resumed = Session(
            _agent_name(), directory=".", session_id="verify-session",
            model=StubModel(reply="second"), checkpoints=checkpoints,
        )
        restored = await resumed.restore()

        await session.aclose()
        await resumed.aclose()

        residue = {
            key: sorted(str(path.relative_to(root)) for path in Path(root).rglob("*") if path.is_file())
            for key, root in roots.items()
        }
        observations.update({
            "answer": answer,
            "used_caller_model": answer == "the stub model answered",
            "observations_recorded": len(seen),
            "checkpoint_saved": bool(saved),
            "resumed": restored,
            "resumed_messages": len(resumed.conversation),
            "disk_residue": residue,
        })
        passed = (
            answer == "the stub model answered"
            and bool(seen)
            and bool(saved)
            and restored
            and len(resumed.conversation) >= 2
            and all(not files for files in residue.values())
            and "checkpoints:" in observations["rejects_incomplete_port"]
        )
        return Outcome("library", passed, observations=observations)

    try:
        return asyncio.run(drive())
    finally:
        discard(roots)


def fan_out() -> Outcome:
    """Twelve sessions cost closer to one prototype than to twelve workers.

    Measured against **proportional set size**, which is the only honest accounting when pages
    are shared: RSS counts every shared page in full in every process that maps it, so twelve
    forks of a 260 MB parent each *look* like 260 MB and the total looks like 3 GB. PSS divides
    a shared page by the number of processes mapping it, which is what actually answers "what
    does one more session cost".

    Linux exposes it in `/proc/<pid>/smaps_rollup`. macOS has no equivalent, so there the stage
    reports the spawn timing — which is the other half of the claim — and says the memory half
    was not measurable rather than pretending otherwise.
    """
    roots = make_roots()
    seed_configuration(roots, PERMISSIVE_CONFINEMENT)
    workspace = tempfile.mkdtemp(prefix="daisy-verify-fan-")

    def proportional_kilobytes(pid: int) -> int:
        try:
            for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
                if line.startswith("Pss:"):
                    return int(line.split()[1])
        except (OSError, ValueError, IndexError):
            return 0
        return 0

    def resident_kilobytes(pid: int) -> int:
        result = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
        try:
            return int(result.stdout.strip() or 0)
        except ValueError:
            return 0

    try:
        with daemon(roots=roots) as instance:
            status = instance.await_prototype()
            prototype_pid = int(status.get("pid") or 0)
            if not prototype_pid:
                return Outcome("fan-out", False, reason="the prototype never reported a process")

            # Taken before any fork, while the prototype is the only process mapping its
            # image: this is what one cold-started worker would cost on its own, and it is the
            # only honest baseline to model twelve spawns against.
            alone_kilobytes = proportional_kilobytes(prototype_pid)

            started = time.monotonic()
            sessions = [
                instance.call("session.create", {"agent": _agent_name(), "working_directory": workspace})
                for _ in range(12)
            ]
            elapsed = time.monotonic() - started

            children: list[int] = []
            for session in sessions:
                record = instance.call("session.get", {"id": session["id"]})["session"]
                pid = int(record.get("pid") or 0)
                if pid:
                    children.append(pid)

            proportional = [proportional_kilobytes(pid) for pid in children]
            measurable = all(proportional) and bool(proportional)
            observations: dict[str, Any] = {
                "sessions": len(sessions),
                "seconds_total": round(elapsed, 2),
                "seconds_each": round(elapsed / max(1, len(sessions)), 3),
                "resident_mb_each": round(resident_kilobytes(children[0]) / 1024, 1) if children else 0,
            }

            if measurable and alone_kilobytes:
                fleet = proportional_kilobytes(prototype_pid) + sum(proportional)
                marginal = sum(proportional) / len(proportional)
                # What twelve cold starts would have cost: each one a whole interpreter, plus
                # the prototype itself.
                modelled_spawn = alone_kilobytes * (len(children) + 1)
                observations.update({
                    "one_worker_alone_mb": round(alone_kilobytes / 1024, 1),
                    "marginal_pss_mb_per_session": round(marginal / 1024, 1),
                    "fleet_pss_mb": round(fleet / 1024, 1),
                    "modelled_by_spawn_mb": round(modelled_spawn / 1024, 1),
                    "saving_percent": round(100 * (1 - fleet / modelled_spawn), 1),
                })
                # The claim being checked is the *fleet* one, because that is the one the
                # design rests on: N sessions must cost far less than N interpreters. The
                # marginal figure is reported rather than asserted on, because it depends on
                # how much work each session has done — an idle worker and one mid-turn are
                # legitimately different numbers, and a threshold on it would be a threshold on
                # the test's own timing.
                cheap_enough = observations["saving_percent"] >= 60.0
            else:
                observations["memory"] = "PSS is not available on this platform; timing only"
                cheap_enough = True

            passed = len(children) == 12 and elapsed / max(1, len(sessions)) < 5.0 and cheap_enough
            return Outcome("fan-out", passed, observations=observations)
    finally:
        discard(roots)
        subprocess.run(["rm", "-rf", workspace], check=False)


def macos_fork() -> Outcome:
    """The three questions only macOS can answer, and the invariant only mach can measure.

    Whether a forked child may initialise CoreFoundation, whether the TCC Accessibility grant
    follows a fork, and whether `sandbox-exec` still works from a child — plus the native
    thread count, which `threading.enumerate()` cannot see and which is what actually decides
    whether the fork is legal.

    Skipped rather than failed elsewhere. Linux passing this would mean nothing: the failure it
    guards against is an abort inside the Objective-C runtime, and the invariant has already
    been broken once by an import-time HTTP call that broke nothing at all on Linux.
    """
    if sys.platform != "darwin":
        return Outcome(
            "macos-fork", True, skipped=True,
            reason=f"macOS only; this is {sys.platform}",
        )
    probe = Path(__file__).resolve().parent / "macos_fork.py"
    result = subprocess.run(
        [sys.executable, str(probe)],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(SOURCE_ROOT)},
    )
    return Outcome(
        "macos-fork", result.returncode == 0,
        reason="" if result.returncode == 0 else "the fork probe reported a failure",
        observations={"output": (result.stdout + result.stderr)[-3000:]},
    )


def artifact_wipe() -> Outcome:
    """The artifact surface is gone, and the A2A deliverable that shares its name is not.

    Checked by naming the deleted things rather than by grepping for the word. "Artifact" is
    also the protocol's name for a turn's result — written by `updater.add_artifact` into
    `turn_artifacts` — and a check that could not tell those apart would either fail forever or
    have to be weakened until it proved nothing.
    """
    root = SOURCE_ROOT.parent

    # Identifiers that existed only in the deleted feature.
    deleted_symbols = (
        "open_artifact", "ARTIFACT_EVENT_KIND", "artifact_maximum_bytes",
        "ArtifactVersionRecord", "ArtifactFileRecord", "ArtifactSurfaceRecord",
        "ArtifactAnnotationRecord", "artifact_runtime", "proxy_runtime",
        "annotation_stamping", "browser_assets", "capture_artifacts",
        "set_artifact_capture", "_capture_written_artifacts", "artifactPageUrl",
        "artifact-proxy-ws", "artifact_image_annotations", "ArtifactImageAnnotation",
        "artifact_update_mode", "sendArtifactEvent", "activeArtifactId",
    )
    # Files the deletion took, which must not have come back.
    deleted_files = (
        "src/daisy/daemon/persistence/artifacts.py",
        "src/daisy/daemon/persistence/versioning.py",
        "src/daisy/rest/routes/artifacts.py",
        "src/daisy/rest/services/proxy.py",
        "src/daisy/runtime/annotation_stamping.py",
        "src/daisy/base/browser_assets.py",
        "src/daisy/daemon/pool.py",
        "web/src/components/artifact-history.tsx",
        "web/src/components/artifact-bridge.tsx",
        "web/src/lib/artifact-annotations.ts",
    )
    # And the deliverable, which must still be there: the protocol's own name for a turn's
    # result. Deleting it would delete the result and take A2A compliance with it.
    required = {
        "src/daisy/daemon/persistence/turn_store.py": "turn_artifacts",
        "src/daisy/worker/turn.py": "add_artifact",
    }

    offenders: list[str] = []
    for directory in (root / "src", root / "web" / "src"):
        for path in directory.rglob("*"):
            if path.suffix not in (".py", ".ts", ".tsx", ".md", ".json") or "__pycache__" in path.parts:
                continue
            text = path.read_text(errors="ignore")
            for number, line in enumerate(text.splitlines(), 1):
                for symbol in deleted_symbols:
                    if symbol in line:
                        offenders.append(f"{path.relative_to(root)}:{number} {symbol}")

    resurrected = [name for name in deleted_files if (root / name).exists()]
    missing = [
        f"{path}:{symbol}" for path, symbol in required.items()
        if symbol not in (root / path).read_text(errors="ignore")
    ]

    observations = {
        "stray_symbols": offenders[:20],
        "stray_count": len(offenders),
        "resurrected_files": resurrected,
        "deliverable_intact": not missing,
    }
    return Outcome("artifact-wipe", not offenders and not resurrected and not missing,
                   observations=observations)


STAGES = {
    "structure": structure,
    "reentrancy": reentrancy,
    "artifact-wipe": artifact_wipe,
    "library": library_ports,
    "prototype": prototype,
    "macos-fork": macos_fork,
    "daemon": daemon_boot,
    "session": session_lifecycle,
    "reaping": reaping,
    "prototype-death": prototype_death,
    "fan-out": fan_out,
}

__all__ = ["STAGES", "Outcome"]
