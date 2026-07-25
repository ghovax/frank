"""Run a ``control_screen`` script in a killable subprocess and bridge its primitive calls back to
the live surface. The model's Python runs in :mod:`xeac.computer.control_child` — a disposable
child that holds no state — and every ``click``/``type``/``evaluate`` it makes arrives here as a
JSON request, is performed against the real surface on its serial worker (trusted input, full
actionability), and is answered. A wall-clock timeout kills the child; rlimits bound its CPU and
memory; a crash or runaway loop dies with it and never touches the server.

The bridge is generic over a ``dispatch`` coroutine ``(name, args, kwargs) -> result`` so the
executor can be exercised without a browser or a Mac in the loop — the surface wiring lives in the
tool handler, not here.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Awaitable, Callable, Optional

from xeac.computer.surface import message_loader
from xeac.base.serialization import compact

# Model-facing control messages live in messages/control/*.md, loaded here so the child (which
# holds no XEAC code) can report bare facts and leave the prose to the loader.
message = message_loader("control")

# Defaults for the child's resource ceilings, sized so a normal script never notices them and a
# pathological one dies before it can hurt the host. The wall-clock timeout is the hard stop.
_DEFAULT_TIMEOUT_SECONDS = 120.0
_DEFAULT_ADDRESS_SPACE_BYTES = 2 * 1024 * 1024 * 1024

Dispatch = Callable[[str, list, dict], Awaitable[Any]]


async def run_control_script(
    script: str,
    dispatch: Dispatch,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    address_space_bytes: int = _DEFAULT_ADDRESS_SPACE_BYTES,
) -> dict:
    """Execute ``script`` in a child process, servicing its primitive calls via ``dispatch``, and
    return the child's result dict (``{ok, value?, stdout?, error?, traceback?}``). On timeout the
    child is killed and a timeout payload is returned instead."""
    request_read, request_write = os.pipe()   # child → parent (primitive calls)
    reply_read, reply_write = os.pipe()        # parent → child (configuration, then results)

    configuration = {
        "script": script,
        "limits": {"cpu_seconds": int(timeout) + 5, "address_space_bytes": address_space_bytes},
    }

    # Launch the child by file path, not ``-m xeac.computer.control_child``: running it as a module
    # would import the ``xeac.computer`` package first, which pulls the macOS-only surface code. As
    # a script it stays stdlib-only, which is the whole point of the disposable child. The two pipe
    # fds are passed on argv (not via the environment, which would leak them into every subprocess);
    # the configuration is handed over on the reply pipe below.
    child_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "control_child.py")
    process = await asyncio.create_subprocess_exec(
        sys.executable, child_path, str(request_write), str(reply_read),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        pass_fds=(request_write, reply_read),
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
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await _drain(process)
        return {"ok": False, "error": f"control_screen: the script exceeded its {int(timeout)}s time limit and was stopped."}
    finally:
        pump_task.cancel()
        _quietly_close(requests)
        _quietly_close(replies)

    result = _parse_result(stdout)
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


def _parse_result(stdout: Optional[bytes]) -> dict:
    text = (stdout or b"").decode("utf-8", "replace").strip()
    if not text:
        return {"ok": False, "error": "control_screen: the script produced no result."}
    try:
        return json.loads(text)
    except Exception:
        # The child always writes JSON last; anything else is a hard crash (segfault, OOM kill).
        return {"ok": False, "error": "control_screen: the script process died before returning a result.", "output": text[-2000:]}
