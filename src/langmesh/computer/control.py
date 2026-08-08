"""Run a `control_screen` script in a killable subprocess and bridge its primitive calls back to the live surface."""

from __future__ import annotations

import asyncio
import logging
import json
import os
import shutil
import sys
from typing import Any, Awaitable, Callable, Optional

from langmesh.base import confinement
from langmesh.computer.surface import message_loader
from langmesh.base.serialization import compact
from langmesh.base.tuning import Tunable, active_tuning

logger = logging.getLogger("langmesh.computer.control")

# Model-facing control messages live in their own folder, so the child reports bare facts and the prose is loaded here.
message = message_loader("control")


#: The role name the frozen executable answers to for this child, dispatched before the runtime is imported.
CONTROL_CHILD_ROLE = "control-child"


def _child_command(request_write: int, reply_read: int) -> list[str]:
    """How to launch the disposable child, from a checkout and from the packaged app alike."""
    numbers = [str(request_write), str(reply_read)]
    if getattr(sys, "frozen", False):
        return [sys.executable, CONTROL_CHILD_ROLE, *numbers]
    child_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "control_child.py")
    return [sys.executable, child_path, *numbers]


class _NotPermitted(Exception):
    """A primitive this session may not run, given its own type so the pump can answer with what is available."""


def _script_ceiling() -> float:
    """The child's wall-clock limit, and the base of a stack the surface's guard and worker thread sit above."""
    return active_tuning().duration(Tunable.control_script)


Dispatch = Callable[[str, list, dict], Awaitable[Any]]


async def run_control_script(
    script: str,
    dispatch: Dispatch,
    *,
    timeout: Optional[float] = None,
    profile: Any = None,
    workspace: str = "",
    primitives: Optional[tuple[str, ...]] = None,
    target: str = "",
    import_roots: Optional[list[str]] = None,
    dependency_roots: Optional[list[str]] = None,
    library_roots: Optional[list[str]] = None,
) -> dict:
    """Execute `script` in a child process, servicing its primitive calls, and return the child's result."""
    timeout = timeout if timeout is not None else _script_ceiling()
    permitted = frozenset(primitives or ())
    request_read, request_write = os.pipe()  # child to parent (primitive calls)
    reply_read, reply_write = os.pipe()  # parent to child (configuration, then results)

    configuration = {
        "script": script,
        # Which names exist in the script's namespace, decided by the surface because only it knows what it can do.
        "primitives": list(primitives or ()),
        # The place the script drives, so the child hands it a bound `screen` rather than repeating the target per call.
        "target": target,
        # The workflow directories, on the child's import path so a saved workflow is importable by name.
        "import_roots": list(import_roots or ()),
        # The environments those projects were installed into, appended so they are reachable without deciding imports.
        "dependency_roots": list(dependency_roots or ()),
        # Directories of shared libraries, sent as paths because macOS strips every `DYLD_*` variable from a signed child.
        "library_roots": list(library_roots or ()),
        # CPU seconds only: address space is the confinement profile's, so one mechanism owns each limit.
        "limits": {"cpu_seconds": int(timeout) + 5},
    }

    # The two pipe descriptors go on argv rather than the environment, which would leak them into every subprocess.
    child_command = _child_command(request_write, reply_read)
    # Strictly less than the session holds: everything this child does is bridged over the pipes, so it needs nothing itself.
    scratch = confinement.private_scratch(profile, workspace=workspace, prefix="langmesh-screen-")
    child_profile = (
        profile.narrowed(writable=[scratch] if scratch else [], network=False, workspace=workspace)
        if profile is not None
        else None
    )
    spawn = confinement.spawn_recipe(
        confinement.first_attempt(child_profile, workspace=workspace),
        workspace=workspace,
        # No permitted scratch means the child is told about none, rather than pointed at a directory it was refused.
        extra_environment={"TMPDIR": scratch, "PWD": scratch} if scratch else None,
    )
    process = await asyncio.create_subprocess_exec(
        *spawn.prefix,
        *child_command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        pass_fds=(request_write, reply_read),
        env=spawn.environment,
        preexec_fn=spawn.preexec,
        # Its own scratch directory, since the narrowing has just taken the inherited working directory out of its readable set.
        cwd=scratch or None,
    )
    # The parent keeps only its own ends; the child holds the others.
    os.close(request_write)
    os.close(reply_read)
    requests = os.fdopen(request_read, "r")
    replies = os.fdopen(reply_write, "w", buffering=1)
    # The configuration is the first line the child reads on the reply pipe; primitive replies follow.
    _write_line(replies, compact(configuration))

    async def pump() -> None:
        """Service one primitive call at a time until the child closes the request pipe."""
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, requests.readline)
            if not line:
                return
            try:
                call = json.loads(line)
                name = call["call"]
                # The permitted set is checked where it arrives, rather than left to whatever dispatch a caller passed.
                if permitted and name not in permitted:
                    raise _NotPermitted(name)
                value = await dispatch(name, call.get("args", []), call.get("kwargs", {}))
                reply: Any = {"value": value}
            except _NotPermitted as refusal:
                reply = {
                    "error": message(
                        "primitive_not_permitted",
                        primitive=str(refusal),
                        available=", ".join(sorted(permitted)),
                    )
                }
            except (
                Exception
            ) as error:  # a failed primitive is raised into the script, not fatal here
                reply = {"error": f"{type(error).__name__}: {error}"}
            await loop.run_in_executor(None, _write_line, replies, compact(reply, default=str))

    pump_task = asyncio.create_task(pump())
    try:
        # Keep stderr, because a child that died before writing its JSON says why there and nowhere else.
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await _drain(process)
        return {
            "ok": False,
            "error": f"control_screen: the script exceeded its {int(timeout)}s time limit and was stopped.",
        }
    finally:
        pump_task.cancel()
        _quietly_close(requests)
        _quietly_close(replies)
        # The scratch directory was made for this run, and a directory per call would otherwise accumulate.
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)

    result = _parse_result(stdout, stderr, process.returncode)
    if result.get("error_code") == "syntax_error":
        # The interpreter's rendering when there is one, since the line and the caret are what the error is fixed from.
        rendered = str(result.get("rendered") or "").strip()
        if not rendered:
            rendered = f"SyntaxError: {result.get('detail', '')} (line {result.get('line', '')})"
        return {"ok": False, "error": message("syntax_error", rendered=rendered)}
    if result.get("error_code") == "needs_import":
        return {"ok": False, "error": message("needs_import", detail=str(result.get("detail", "")))}
    return result


def _write_line(stream: Any, text: str) -> None:
    stream.write(text + "\n")
    stream.flush()


async def _drain(process: Any) -> None:
    try:
        await asyncio.wait_for(process.communicate(), timeout=5.0)
    except Exception:
        pass


def _quietly_close(stream: Any) -> None:
    try:
        stream.close()
    except Exception:
        pass


# Recognised ways the child dies before it can speak, each with its own remedy and so its own message.
def _explain_silent_exit(complaint: str) -> str:
    lowered = complaint.lower()
    if "operation not permitted" in lowered or "sandbox" in lowered:
        return "The screen-control helper could not start because the sandbox refused to run it. Screen control needs the helper to be executable inside the session's sandbox; check the sandbox settings for this project, or turn enforcement off to confirm that is the cause."
    if "accessibility" in lowered or "axapi" in lowered or "not trusted" in lowered:
        return "The screen-control helper could not read the screen because macOS Accessibility is not granted. Grant it in Settings, then try again."
    if not complaint:
        return "The screen-control helper stopped before it could report anything, and said nothing about why — it was most likely killed as it started."
    # The child's own words rather than a summary of them, since that is what lets a script be fixed.
    return (
        f"The screen-control helper stopped before it could report a result. It said:\n{complaint}"
    )


def _parse_result(
    stdout: Optional[bytes], stderr: Optional[bytes] = None, exit_code: Optional[int] = None
) -> dict:
    text = (stdout or b"").decode("utf-8", "replace").strip()
    complaint = (stderr or b"").decode("utf-8", "replace").strip()
    if not text:
        # The child writes its JSON last, so empty stdout means it never got there.
        logger.warning(
            "control_screen produced no result (exit code %s): %s",
            exit_code,
            complaint[-2000:] or "(nothing on stderr either)",
        )
        # The child's own words go to the log rather than into the answer, which needs a sentence a person can act on.
        return {"ok": False, "error": _explain_silent_exit(complaint), "exit_code": exit_code}
    try:
        return json.loads(text)
    except Exception:
        # The child always writes JSON last; anything else is a hard crash (segfault, OOM kill).
        logger.warning(
            "control_screen returned unparseable output (exit code %s): %s", exit_code, text[-500:]
        )
        return {
            "ok": False,
            "error": "The screen-control script stopped before it finished.",
            "output": text[-2000:],
        }
