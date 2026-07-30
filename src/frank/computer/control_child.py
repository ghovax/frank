"""The child process that runs a ``control_screen`` script. It holds no Frank code and no surface
state: every primitive the script calls (``click``, ``type``, ``evaluate``, …) is a stub that
sends one JSON request to the parent, blocks for the JSON reply, and returns it. The parent
performs the real, trusted action on the live surface and answers. Keeping the child this thin is
what makes it disposable — a runaway loop or a crash dies here without touching the worker, and a
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

# The primitives a script may call, sent by the parent because only the parent knows which surface
# is answering. A name the surface does not implement is simply not bound, so reaching for it is a
# `NameError` at the line that used it, raised before anything else in the script has run.
#
# It used to be one fixed tuple — the union of both surfaces — handed out whole regardless of what
# was on the other end. A native window implements eight of the twenty, so a script could call
# `hover`, `evaluate` or `tabs` and learn at runtime, from a result payload, that the plan it had
# already committed to was never possible. A result payload reads as a runtime condition worth
# working around; an unbound name reads as what it is.
_FALLBACK_PRIMITIVES = ("find_one", "find_many", "click", "type", "press", "scroll", "drag",
                        "select", "caret", "read")

# The request/reply pipes to the parent, opened in ``main`` so importing this module (it never
# should be — it is a script) has no side effect.
_request: Any = None
_reply: Any = None


def _script_namespace(allowed: tuple, target: str, workspace: str) -> dict[str, Any]:
    """What a script starts with: a bound ``screen``, and its own workflows on the import path.

    One calling form, here and in a saved file. The primitives used to be injected as bare names,
    which reads pleasantly and cannot be written down anywhere else — a script saved to disk was
    not a Python program, because nothing defined ``click``. Now the same call is
    ``screen.click(...)`` whether it is typed into a tool argument or imported from a module a
    person wrote last month, and there is one vocabulary rather than one for each situation.

    ``frank.screen`` is the single Frank module this child imports. It carries no surface state and
    no configuration — just the object and the hook this installs — so the child stays the thin,
    disposable thing it was built to be."""
    from frank import screen as screen_module

    screen_module.install_bridge(lambda name, arguments, keywords: _perform(name, arguments, keywords))
    place = screen_module.Screen(target)
    # A project's own workflows are importable by name, so `from workflows.invoice import run`
    # reaches the file the person wrote rather than needing its text pasted into the script.
    if workspace and workspace not in sys.path:
        sys.path.insert(0, workspace)
    namespace: dict[str, Any] = {"screen": place, "Screen": screen_module.Screen}
    # The names a surface implements are still reported, so an attribute the place does not have
    # fails against the live surface, which can say what it does have.
    namespace["__primitives__"] = tuple(allowed)
    return namespace


def _apply_limits(limits: dict[str, int]) -> None:
    """Bound CPU seconds, best effort — a runaway computation dies on its own even
    before the parent's wall-clock kill, and a memory bomb cannot take the host down."""
    try:
        import resource

        cpu_seconds = limits.get("cpu_seconds")
        if cpu_seconds:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
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


def _perform(name: str, arguments: list, keywords: dict) -> Any:
    """One screen call, bridged to the parent. Installed into `frank.screen` so every call a
    script makes — inline or through an imported workflow — travels the same wire."""
    return _call(name, tuple(arguments), keywords)


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

    allowed = configuration.get("primitives") or _FALLBACK_PRIMITIVES
    namespace: dict[str, Any] = _script_namespace(allowed, configuration.get("target", ""),
                                                  configuration.get("workspace", ""))
    captured = io.StringIO()
    result: dict[str, Any] = {"ok": True}
    try:
        with redirect_stdout(captured):
            result["value"] = _run(script, namespace)
    except SyntaxError as error:
        # The child holds no Frank code, so it reports the bare facts; the parent renders the
        # model-facing message (messages/control/syntax_error.md) from them.
        result = {"ok": False, "error_code": "syntax_error", "detail": error.msg or "", "line": error.lineno or 0}
    except Exception as error:
        result = {"ok": False, "error": f"{type(error).__name__}: {error}", "traceback": traceback.format_exc(limit=8)}
    output = captured.getvalue()
    if output:
        result["stdout"] = output
    try:
        # Compact, like every other payload that ends up in front of a model — spelled out
        # here rather than imported from `base`, because this file is launched by path and
        # holds no Frank code. One helper's worth of duplication is the price of that.
        sys.stdout.write(json.dumps(result, default=str, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        sys.stdout.write(json.dumps(
            {"ok": False, "error": "control_screen: the result was not serialisable."},
            separators=(",", ":"),
        ))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
