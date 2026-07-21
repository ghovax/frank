"""Run a ``control_screen`` script in a killable subprocess and bridge its primitive calls back to
the live surface. The model's Python runs in :mod:`daisy.computer.control_child` — a disposable
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
    return the child's result dict (``{ok, return?, stdout?, error?, traceback?}``). On timeout the
    child is killed and a timeout payload is returned instead."""
    request_read, request_write = os.pipe()   # child → parent (primitive calls)
    reply_read, reply_write = os.pipe()        # parent → child (results)

    config = {
        "script": script,
        "limits": {"cpu_seconds": int(timeout) + 5, "address_space_bytes": address_space_bytes},
    }
    environment = {
        **os.environ,
        "DAISY_CONTROL_CONFIG": json.dumps(config),
        "DAISY_CONTROL_REQUEST_FD": str(request_write),
        "DAISY_CONTROL_REPLY_FD": str(reply_read),
    }

    # Launch the child by file path, not ``-m daisy.computer.control_child``: running it as a
    # module would import the ``daisy.computer`` package first, which pulls the macOS-only surface
    # code. As a script it stays stdlib-only, which is the whole point of the disposable child.
    child_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "control_child.py")
    process = await asyncio.create_subprocess_exec(
        sys.executable, child_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=environment, pass_fds=(request_write, reply_read),
    )
    # The parent keeps only its own ends; the child holds the others.
    os.close(request_write)
    os.close(reply_read)
    requests = os.fdopen(request_read, "r")
    replies = os.fdopen(reply_write, "w", buffering=1)

    async def pump() -> None:
        """Service one primitive call at a time until the child closes the request pipe."""
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, requests.readline)
            if not line:
                return
            try:
                call = json.loads(line)
                result = await dispatch(call["call"], call.get("args", []), call.get("kwargs", {}))
                reply: Any = result
            except Exception as error:  # a failed primitive is reported into the script, not fatal
                reply = {"__error__": f"{type(error).__name__}: {error}"}
            await loop.run_in_executor(None, _write_line, replies, json.dumps(reply, default=str))

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

    return _parse_result(stdout)


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
