"""Run a ``control_screen`` script in a killable subprocess and bridge its primitive calls back to
the live surface. The model's Python runs in :mod:`frank.computer.control_child` — a disposable
child that holds no state — and every ``click``/``type``/``evaluate`` it makes arrives here as a
JSON request, is performed against the real surface on its serial worker (trusted input, full
actionability), and is answered. A wall-clock timeout kills the child; rlimits bound its CPU and
memory; a crash or runaway loop dies with it and never touches the worker.

The bridge is generic over a ``dispatch`` coroutine ``(name, args, kwargs) -> result`` so the
executor can be exercised without a browser or a Mac in the loop — the surface wiring lives in the
tool handler, not here.
"""
from __future__ import annotations

import asyncio
import logging
import json
import os
import sys
from typing import Any, Awaitable, Callable, Optional

from frank.base import confinement
from frank.computer.surface import message_loader
from frank.base.serialization import compact
from frank.base.tuning import Tunable, active_tuning

logger = logging.getLogger("frank.computer.control")

# Model-facing control messages live in messages/control/*.md, loaded here so the child (which
# holds no Frank code) can report bare facts and leave the prose to the loader.
message = message_loader("control")

def _script_ceiling() -> float:
    """The child's wall-clock limit, and the base of an ordered stack.

    The surface's guard and its worker thread each sit a margin above this, so a script can
    never outlive the machinery waiting on it. They used to be three independent constants that
    happened to be equal, which meant raising one made the guard fire first, drop the connection
    and leave the surface half-dead."""
    return active_tuning().duration(Tunable.control_script_seconds)

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
) -> dict:
    """Execute ``script`` in a child process, servicing its primitive calls via ``dispatch``, and
    return the child's result dict (``{ok, value?, stdout?, error?, traceback?}``). On timeout the
    child is killed and a timeout payload is returned instead."""
    timeout = timeout if timeout is not None else _script_ceiling()
    request_read, request_write = os.pipe()   # child → parent (primitive calls)
    reply_read, reply_write = os.pipe()        # parent → child (configuration, then results)

    configuration = {
        "script": script,
        # Which names exist in the script's namespace. The surface decides, because the surface is
        # the only thing that knows what it can do.
        "primitives": list(primitives or ()),
        # The place the script drives, so the child can hand it a bound `screen` rather than
        # making every call name a target the tool argument already settled.
        "target": target,
        # The workflow directories, put on the child's import path so a workflow somebody saved
        # is importable by name instead of having to be pasted in as text.
        "import_roots": list(import_roots or ()),
        # CPU seconds only. Address space is the confinement profile's `RLIMIT_AS` now — this
        # used to set it too, from its own constant, so two mechanisms raced for one rlimit.
        "limits": {"cpu_seconds": int(timeout) + 5},
    }

    # Launch the child by file path, not ``-m frank.computer.control_child``: running it as a module
    # would import the ``frank.computer`` package first, which pulls the macOS-only surface code. As
    # a script it stays stdlib-only, which is the whole point of the disposable child. The two pipe
    # fds are passed on argv (not via the environment, which would leak them into every subprocess);
    # the configuration is handed over on the reply pipe below.
    child_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "control_child.py")
    # Strictly less than the session holds. Everything this child can do is bridged to this
    # process over the two pipes below — a click, a find, an evaluate are all JSON requests the
    # parent performs — so it needs no network at all and nowhere to write but a temporary
    # directory. Derived from the session's profile rather than configured separately: two
    # profiles to configure would be two profiles to get wrong.
    scratch = confinement.temporary_directory(profile, workspace=workspace)
    child_profile = (
        profile.narrowed(writable=[scratch] if scratch else [], network=False, workspace=workspace)
        if profile is not None else None
    )
    spawn = confinement.spawn_recipe(
        child_profile, workspace=workspace,
        # No permitted scratch means the child is told about none. A profile that grants no
        # writable directory should produce a child that cannot write, not one pointed at a
        # directory the session itself was refused.
        extra_environment={"TMPDIR": scratch} if scratch else None,
    )
    process = await asyncio.create_subprocess_exec(
        *spawn.prefix, sys.executable, child_path, str(request_write), str(reply_read),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        pass_fds=(request_write, reply_read),
        env=spawn.environment,
        preexec_fn=spawn.preexec,
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
                value = await dispatch(call["call"], call.get("args", []), call.get("kwargs", {}))
                reply: Any = {"value": value}
            except Exception as error:  # a failed primitive is raised into the script, not fatal here
                reply = {"error": f"{type(error).__name__}: {error}"}
            await loop.run_in_executor(None, _write_line, replies, compact(reply, default=str))

    pump_task = asyncio.create_task(pump())
    try:
        # Keep stderr. It used to be discarded into `_`, so a child that died before it could
        # write its JSON — no Accessibility grant, a sandbox denial, a failed import — was
        # reported as "produced no result" with the one line that said why thrown away, and
        # nothing in the log either. The reason the child gives is the whole diagnosis.
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await _drain(process)
        return {"ok": False, "error": f"control_screen: the script exceeded its {int(timeout)}s time limit and was stopped."}
    finally:
        pump_task.cancel()
        _quietly_close(requests)
        _quietly_close(replies)

    result = _parse_result(stdout, stderr, process.returncode)
    if result.get("error_code") == "syntax_error":
        return {"ok": False, "error": message("syntax_error", detail=str(result.get("detail", "")), line=str(result.get("line", "")))}
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


# Recognised ways the child dies before it can speak. Each is a real condition with a
# different remedy, so each says its own thing rather than all of them sharing one apology.
def _explain_silent_exit(complaint: str) -> str:
    lowered = complaint.lower()
    if "operation not permitted" in lowered or "sandbox" in lowered:
        return (
            "The screen-control helper could not start because the sandbox refused to run it. "
            "Screen control needs the helper to be executable inside the session's sandbox; "
            "check the sandbox settings for this project, or turn enforcement off to confirm "
            "that is the cause."
        )
    if "accessibility" in lowered or "axapi" in lowered or "not trusted" in lowered:
        return (
            "The screen-control helper could not read the screen because macOS Accessibility "
            "is not granted. Grant it in Settings, then try again."
        )
    if not complaint:
        return (
            "The screen-control helper stopped before it could report anything, and said "
            "nothing about why — it was most likely killed as it started."
        )
    # The child's own words, not a summary of them. This used to point at a log the model cannot
    # read, discarding the one thing that would have let it fix its script: a `NameError` naming
    # the undefined primitive arrived as "the daemon log says why", so a model that had simply
    # written a name wrong was told its screen access had failed, and concluded it lacked
    # permissions it had all along.
    return f"The screen-control helper stopped before it could report a result. It said:\n{complaint}"


def _parse_result(stdout: Optional[bytes], stderr: Optional[bytes] = None, exit_code: Optional[int] = None) -> dict:
    text = (stdout or b"").decode("utf-8", "replace").strip()
    complaint = (stderr or b"").decode("utf-8", "replace").strip()
    if not text:
        # The child writes its JSON last, so empty stdout means it never got there. Whatever it
        # said on the way out is the reason, and it is reported rather than summarised away.
        logger.warning(
            "control_screen produced no result (exit code %s): %s",
            exit_code, complaint[-2000:] or "(nothing on stderr either)",
        )
        # The child's own words go to the log, where they are wanted, and not into the answer.
        # Handing raw stderr back as the tool's error put a sandbox denial in front of a person
        # as a sentence about a Python executable — true, useless, and not their vocabulary.
        # What reaches the model is what happened and what can be done about it.
        return {"ok": False, "error": _explain_silent_exit(complaint), "exit_code": exit_code}
    try:
        return json.loads(text)
    except Exception:
        # The child always writes JSON last; anything else is a hard crash (segfault, OOM kill).
        logger.warning("control_screen returned unparseable output (exit code %s): %s", exit_code, text[-500:])
        return {"ok": False, "error": "The screen-control script stopped before it finished.", "output": text[-2000:]}
