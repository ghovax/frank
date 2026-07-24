"""The child process that runs a ``control_screen`` script. It holds no XEAC code and no surface
state: every primitive the script calls (``click``, ``type``, ``evaluate``, …) is a stub that
sends one JSON request to the parent, blocks for the JSON reply, and returns it. The parent
performs the real, trusted action on the live surface and answers. Keeping the child this thin is
what makes it disposable — a runaway loop or a crash dies here without touching the server, and a
wall-clock timeout in the parent simply kills it.

The script runs with **REPL semantics**: every statement executes, and the value of a trailing
bare expression (if any) is reported as the result, exactly like the last line of a notebook cell.
Anything the script prints is captured and reported too, so a script that just prints its findings
is as good as one that ends in an expression. The script is run through the AST — no source is
rewritten — so a multiline string literal is never disturbed.

The primitives are injected as bare names (``click(...)``, not ``surface.click(...)``) — the script
reads as plain Python. The two pipe fds and the configuration arrive out-of-band (fds on argv, the
configuration as the first line the parent writes on the reply pipe), so nothing about this run
lives in the process environment.
"""
from __future__ import annotations

import ast
import io
import json
import os
import sys
import traceback
from contextlib import redirect_stdout
from typing import Any

# The primitives available inside a script — the union of what the two surfaces perform, plus the
# two retrieval primitives (find_one, find_many) the parent services from the same surface read.
# Web-only names (evaluate, navigate) are always bound; calling one against a native surface returns
# the parent's error, which is the honest outcome.
_PRIMITIVES = (
    "find_one", "find_many",
    "click", "type", "press", "hover", "scroll", "choose", "upload",
    "drag", "select", "caret", "read", "evaluate", "navigate",
)

# The request/reply pipes to the parent, opened in ``main`` so importing this module (it never
# should be — it is a script) has no side effect.
_request: Any = None
_reply: Any = None


def _apply_limits(limits: dict[str, int]) -> None:
    """Bound CPU seconds and address space, best effort — a runaway computation dies on its own even
    before the parent's wall-clock kill, and a memory bomb cannot take the host down."""
    try:
        import resource

        cpu_seconds = limits.get("cpu_seconds")
        if cpu_seconds:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        address_space = limits.get("address_space_bytes")
        if address_space:
            resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
    except Exception:
        pass


def _call(name: str, arguments: tuple, keywords: dict) -> Any:
    """Send one primitive call to the parent and return its reply. A reply carrying ``__error__`` is
    raised as an exception so the script sees a normal Python error it can try/except."""
    json.dump({"call": name, "args": list(arguments), "kwargs": keywords}, _request)
    _request.write("\n")
    _request.flush()
    line = _reply.readline()
    if not line:
        raise RuntimeError("control_screen: the parent closed the connection.")
    reply = json.loads(line)  # the parent always wraps: {"value": …} on success, {"error": …} on failure
    if "error" in reply:
        raise RuntimeError(reply["error"])
    return reply.get("value")


def _make_primitive(name: str):
    def primitive(*arguments: Any, **keywords: Any) -> Any:
        return _call(name, arguments, keywords)

    primitive.__name__ = name
    return primitive


def _run(script: str, namespace: dict[str, Any]) -> Any:
    """Execute ``script`` and return the value of its trailing expression, or ``None``. Parsed to an
    AST first (so a syntax error is precise and no source is rewritten); if the last statement is a
    bare expression it is evaluated separately for its value, and everything before it is executed."""
    tree = ast.parse(script, mode="exec")
    final_value = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        final_expression = tree.body.pop().value
        if tree.body:
            exec(compile(tree, "<control_screen>", "exec"), namespace)  # noqa: S102 (that is the point)
        final_value = eval(compile(ast.Expression(final_expression), "<control_screen>", "eval"), namespace)  # noqa: S307
    else:
        exec(compile(tree, "<control_screen>", "exec"), namespace)  # noqa: S102
    return final_value


def main() -> None:
    global _request, _reply
    _request = os.fdopen(int(sys.argv[1]), "w", buffering=1)
    _reply = os.fdopen(int(sys.argv[2]), "r")
    configuration = json.loads(_reply.readline())  # the parent writes the configuration first
    _apply_limits(configuration.get("limits", {}))
    script = configuration["script"]

    namespace: dict[str, Any] = {name: _make_primitive(name) for name in _PRIMITIVES}
    captured = io.StringIO()
    result: dict[str, Any] = {"ok": True}
    try:
        with redirect_stdout(captured):
            result["value"] = _run(script, namespace)
    except SyntaxError as error:
        # The child holds no XEAC code, so it reports the bare facts; the parent renders the
        # model-facing message (messages/control/syntax_error.md) from them.
        result = {"ok": False, "error_code": "syntax_error", "detail": error.msg or "", "line": error.lineno or 0}
    except Exception as error:
        result = {"ok": False, "error": f"{type(error).__name__}: {error}", "traceback": traceback.format_exc(limit=8)}
    output = captured.getvalue()
    if output:
        result["stdout"] = output
    try:
        sys.stdout.write(json.dumps(result, default=str))
    except Exception:
        sys.stdout.write(json.dumps({"ok": False, "error": "control_screen: the result was not serialisable."}))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
