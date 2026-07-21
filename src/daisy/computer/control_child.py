"""The child process that runs a ``control_screen`` script. It holds no Daisy code and no surface
state: every primitive the script calls (``click``, ``type``, ``evaluate``, …) is a stub that
sends one JSON request to the parent over fd 3, blocks for the JSON reply on fd 4, and returns it.
The parent performs the real, trusted action on the live surface and answers. Keeping the child
this thin is what makes it disposable — a runaway loop or a crash dies here without touching the
server, and a wall-clock timeout in the parent simply kills it.

The script is run as the body of a function so a bare ``return`` ends it with a value; whatever it
returns, plus anything it printed, comes back to the model. The primitives are injected as bare
names (``click(...)``, not ``surface.click(...)``) — the script reads as plain Python.
"""
from __future__ import annotations

import io
import json
import os
import sys
import traceback
from contextlib import redirect_stdout
from typing import Any

# Requests go out to the parent on one inherited pipe, replies come back on another; their fd
# numbers are handed in by the parent so stdout/stderr stay free for the child's own diagnostics
# and never corrupt the protocol. The pipes are opened in ``main`` so merely importing this module
# (it never should be — it is a script) has no side effect.
_REQUEST: Any = None
_REPLY: Any = None

# The primitives available inside a script. Web-only names (evaluate, navigate) are always bound;
# calling one against a native surface returns the parent's error, which is the honest outcome.
_PRIMITIVES = (
    "click", "type", "press", "scroll", "read", "drag", "choose", "upload",
    "select", "caret", "evaluate", "navigate", "screenshot",
)


def _apply_limits(limits: dict[str, int]) -> None:
    """Bound CPU seconds and address space, best effort — a runaway computation dies on its own
    even before the parent's wall-clock kill, and a memory bomb cannot take the host down."""
    try:
        import resource

        cpu = limits.get("cpu_seconds")
        if cpu:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        memory = limits.get("address_space_bytes")
        if memory:
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    except Exception:
        pass


def _call(name: str, args: tuple, kwargs: dict) -> Any:
    """Send one primitive call to the parent and return its reply. A reply carrying ``__error__``
    is raised as an exception so the script sees a normal Python error it can try/except."""
    json.dump({"call": name, "args": list(args), "kwargs": kwargs}, _REQUEST)
    _REQUEST.write("\n")
    _REQUEST.flush()
    line = _REPLY.readline()
    if not line:
        raise RuntimeError("control_screen: the parent closed the connection.")
    reply = json.loads(line)
    if isinstance(reply, dict) and "__error__" in reply:
        raise RuntimeError(reply["__error__"])
    return reply


def _make_primitive(name: str):
    def primitive(*args: Any, **kwargs: Any) -> Any:
        return _call(name, args, kwargs)

    primitive.__name__ = name
    return primitive


def main() -> None:
    global _REQUEST, _REPLY
    _REQUEST = os.fdopen(int(os.environ["DAISY_CONTROL_REQUEST_FD"]), "w", buffering=1)
    _REPLY = os.fdopen(int(os.environ["DAISY_CONTROL_REPLY_FD"]), "r")
    config = json.loads(os.environ["DAISY_CONTROL_CONFIG"])
    _apply_limits(config.get("limits", {}))
    script = config["script"]

    namespace: dict[str, Any] = {name: _make_primitive(name) for name in _PRIMITIVES}
    # Wrap the script as a function body so a top-level ``return`` is legal and yields a value.
    indented = "\n".join("    " + line for line in script.splitlines())
    wrapper = "def __daisy_script__():\n" + (indented or "    pass") + "\n"

    captured = io.StringIO()
    result: dict[str, Any] = {"ok": True}
    try:
        exec(compile(wrapper, "<control_screen>", "exec"), namespace)  # noqa: S102 (that is the point)
        with redirect_stdout(captured):
            value = namespace["__daisy_script__"]()
        result["return"] = value
    except Exception as error:
        result = {"ok": False, "error": f"{type(error).__name__}: {error}",
                  "traceback": traceback.format_exc(limit=8)}
    output = captured.getvalue()
    if output:
        result["stdout"] = output
    try:
        sys.stdout.write(json.dumps(result, default=str))
        sys.stdout.flush()
    except Exception:
        sys.stdout.write(json.dumps({"ok": False, "error": "control_screen: result was not serialisable."}))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
