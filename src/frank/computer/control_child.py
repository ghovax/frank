"""The child process that runs a ``control_screen`` script."""
from __future__ import annotations

import ast
import io
import json
import os
import sys
import traceback
from contextlib import redirect_stdout
from typing import Any
from frank.base.errors import summary

# The primitives a script may call, sent by the parent because only the parent knows which surface is answering.
_FALLBACK_PRIMITIVES = ("find_one", "find_many", "click", "type", "press", "scroll", "drag",
                        "select", "caret", "read")

# The request/reply pipes to the parent, opened in ``main`` so importing this module (it never should be — it is a script) has no side effect.
_request: Any = None
_reply: Any = None


class _PreloadsBundledLibraries:
    """Loads a package's own shared libraries just before that package is first imported."""

    def __init__(self, directories) -> None:
        self._pending: dict[str, str] = {}
        for directory in directories or ():
            name = os.path.basename(directory)
            # `delocate` puts a wheel's vendored libraries in `<package>/.dylibs`; anything else is the package directory itself.
            package = os.path.basename(os.path.dirname(directory)) if name == ".dylibs" else name
            if package:
                self._pending[package] = directory

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001 — importlib's signature
        directory = self._pending.pop(fullname.split(".")[0], None)
        if directory:
            import ctypes
            import glob

            # `*.dylib`, and `lib*.so` for the ones built with the other extension.
            libraries = (sorted(glob.glob(os.path.join(directory, "*.dylib")))
                         + sorted(glob.glob(os.path.join(directory, "lib*.so"))))
            for library in libraries:
                # Quietly: a library that will not load on its own is not necessarily a problem — it may be one this package never reaches for — and the import that follows is what says whether anything is actually missing.
                try:
                    ctypes.CDLL(library, mode=getattr(ctypes, "RTLD_GLOBAL", 0))
                except OSError:
                    continue
        # Never claims the import. This exists for its side effect; the normal machinery finds the module.
        return None


def _load_screen_module(package_root: str):
    """`frank.screen`, executed on its own, without the package body it lives under."""
    import importlib.util
    import types

    if "frank.screen" in sys.modules:
        return sys.modules["frank.screen"]
    directory = os.path.join(package_root, "frank")
    if "frank" not in sys.modules:
        package = types.ModuleType("frank")
        # The real directory, so `frank.anything_else` still resolves if something asks for it.
        package.__path__ = [directory]
        sys.modules["frank"] = package
    specification = importlib.util.spec_from_file_location(
        "frank.screen", os.path.join(directory, "screen.py")
    )
    if specification is None or specification.loader is None:
        raise ImportError(f"frank.screen could not be loaded from {directory}")
    module = importlib.util.module_from_spec(specification)
    sys.modules["frank.screen"] = module
    specification.loader.exec_module(module)
    return module


def _script_namespace(
    allowed: tuple, target: str, workspace: list, dependencies: list = (), libraries: list = ()
) -> dict[str, Any]:
    """What a script starts with: an empty namespace, and everything importable that it may need."""
    # The path is settled *before* anything is imported, because the import below depends on it.
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [entry for entry in sys.path if os.path.abspath(entry or os.getcwd()) != here]
    package_root = os.path.dirname(os.path.dirname(here))
    if package_root not in sys.path:
        sys.path.insert(0, package_root)

    # `frank.screen`, loaded from its file without running `frank/__init__.py`.
    screen_module = _load_screen_module(package_root)
    screen_module.install_bridge(lambda name, arguments, keywords: _perform(name, arguments, keywords))
    screen_module.screen.target = target
    # A project's and a person's workflow directories, and any script package a skill carries, in precedence order — so `from workflows.invoice import run` reaches whichever wrote it and `from filing import file_all` reaches the skill that ships it.
    for root in reversed(list(workspace or ())):
        if root and root not in sys.path:
            sys.path.insert(0, root)
    # A skill's own dependencies go on the *end*.
    for root in dependencies or ():
        if root and root not in sys.path:
            sys.path.append(root)
    if libraries:
        sys.meta_path.insert(0, _PreloadsBundledLibraries(libraries))
    # The names a surface implements, for a script that wants to ask rather than try.
    return {"__primitives__": tuple(allowed)}


def _apply_limits(limits: dict[str, int]) -> None:
    """Bound CPU seconds, best effort — a runaway computation dies on its own even before the parent's wall-clock kill, and a memory bomb cannot take the host down."""
    try:
        import resource

        cpu_seconds = limits.get("cpu_seconds")
        if cpu_seconds:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    except Exception:
        pass


def _call(name: str, arguments: tuple, keywords: dict) -> Any:
    """Send one primitive call to the parent and return its reply."""
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
    """One screen call, bridged to the parent."""
    return _call(name, tuple(arguments), keywords)


def _run(script: str, namespace: dict[str, Any]) -> Any:
    """Execute ``script`` and return the value of its trailing expression, or ``None``."""
    # Named, so a syntax error reports `File "<control_screen>"` rather than `File "<unknown>"` — the same name the compiles below use, and one that says which script is meant.
    tree = ast.parse(script, filename="<control_screen>", mode="exec")
    final_value = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        final_expression = tree.body.pop().value
        if tree.body:
            exec(compile(tree, "<control_screen>", "exec"), namespace)  # noqa: S102 (that is the point)
        final_value = eval(compile(ast.Expression(final_expression), "<control_screen>", "eval"), namespace)  # noqa: S307
    else:
        exec(compile(tree, "<control_screen>", "exec"), namespace)  # noqa: S102
    return final_value


def _failure(error: BaseException) -> dict[str, Any]:
    """A raised exception as a result: the message once, and the frames that led to it."""
    frames = "".join(traceback.format_tb(error.__traceback__, limit=8)).strip()
    result: dict[str, Any] = {"ok": False, "error": summary(error)}
    if frames:
        result["traceback"] = frames
    return result


def main() -> None:
    global _request, _reply
    _request = os.fdopen(int(sys.argv[1]), "w", buffering=1)
    _reply = os.fdopen(int(sys.argv[2]), "r")
    configuration = json.loads(_reply.readline())  # the parent writes the configuration first
    _apply_limits(configuration.get("limits", {}))
    script = configuration["script"]

    captured = io.StringIO()
    result: dict[str, Any] = {"ok": True}
    try:
        # Building the namespace is inside the guard, because it can fail: it imports `frank.screen` and rewrites `sys.path`, and when it raised, this whole process died before writing anything — so the parent had no result to read and could only say the helper "stopped before it could do anything", which tells a model nothing it can act on.
        allowed = configuration.get("primitives") or _FALLBACK_PRIMITIVES
        namespace: dict[str, Any] = _script_namespace(allowed, configuration.get("target", ""),
                                                      configuration.get("import_roots", []),
                                                      configuration.get("dependency_roots", []),
                                                      configuration.get("library_roots", []))
        with redirect_stdout(captured):
            result["value"] = _run(script, namespace)
    except SyntaxError as error:
        # The interpreter's own rendering, verbatim — the offending line, the caret under the column, the message.
        result = {
            "ok": False, "error_code": "syntax_error",
            "detail": error.msg or "", "line": error.lineno or 0,
            "rendered": "".join(traceback.format_exception_only(type(error), error)).strip(),
        }
    except NameError as error:
        # Almost always the one mistake: a script written as though `screen` were in scope, which it was until this became a program that says where its screen comes from.
        missing = getattr(error, "name", "") or ""
        if missing in ("screen", "Screen", "place"):
            result = {"ok": False, "error_code": "needs_import", "detail": missing}
        else:
            result = _failure(error)
    except Exception as error:
        result = _failure(error)
    output = captured.getvalue()
    if output:
        result["stdout"] = output
    try:
        # Compact, like every other payload that ends up in front of a model — spelled out here rather than imported from `base`, because this file is launched by path and holds no Frank code.
        sys.stdout.write(json.dumps(result, default=str, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        sys.stdout.write(json.dumps(
            {"ok": False, "error": "control_screen: the result was not serialisable."},
            separators=(",", ":"),
        ))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
