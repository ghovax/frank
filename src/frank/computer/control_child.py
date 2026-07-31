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

Nothing is injected into the script's namespace. It reaches the screen by importing it —
``from frank.screen import screen`` — which is what makes the text a program rather than a
fragment that only means something inside one ``exec``: it declares what it depends on, it can be
saved to a file unchanged, and a person can read it without knowing this harness exists. The two
pipe fds and the configuration arrive out-of-band (fds on argv, the configuration as the first
line the parent writes on the reply pipe), so nothing about this run lives in the process
environment.

The script may import anything else it likes. That is not an oversight: this process is confined
by the operating system to a scratch directory and no network, so what it can *reach* is settled
before it starts and does not depend on guessing from the source which modules are harmless. The
screen itself is the exception, and it is bridged rather than held — every primitive call goes to
the parent, which is where the permission to make it was asked for.
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


class _PreloadsBundledLibraries:
    """Loads a package's own shared libraries just before that package is first imported.

    A wheel that ships a compiled library links it as ``@rpath/libwhatever.dylib`` and records
    rpaths that assume the environment it was installed into. Borrowing the packages does not
    reproduce that, so the extension module loads and its library does not, and the import fails
    naming a ``.dylib`` the script never mentioned.

    The ordinary answer is ``DYLD_FALLBACK_LIBRARY_PATH``, and it cannot be used here: macOS
    strips every ``DYLD_*`` variable when the protected ``/usr/bin/sandbox-exec`` is executed, so
    it never reaches this process. Loading the library by absolute path has the same effect from
    the inside — it is registered under its install name, and the later ``@rpath`` request finds
    the image already loaded.

    Done as a finder rather than up front so it costs nothing until it is needed. A script that
    never touches the package never loads its libraries, which matters when one of them is tens
    of megabytes and most scripts want neither.
    """

    def __init__(self, directories) -> None:
        self._pending: dict[str, str] = {}
        for directory in directories or ():
            name = os.path.basename(directory)
            # `delocate` puts a wheel's vendored libraries in `<package>/.dylibs`; anything else
            # is the package directory itself.
            package = os.path.basename(os.path.dirname(directory)) if name == ".dylibs" else name
            if package:
                self._pending[package] = directory

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001 — importlib's signature
        directory = self._pending.pop(fullname.split(".")[0], None)
        if directory:
            import ctypes
            import glob

            # `*.dylib`, and `lib*.so` for the ones built with the other extension. The `lib`
            # prefix is what separates a shared library from a Python extension module: they are
            # both `.so` here, and loading `_extra.so` this way would be wrong. Dylibs first,
            # since a `lib*.so` in these bundles is usually the C++ layer over the C one.
            libraries = (sorted(glob.glob(os.path.join(directory, "*.dylib")))
                         + sorted(glob.glob(os.path.join(directory, "lib*.so"))))
            for library in libraries:
                # Quietly: a library that will not load on its own is not necessarily a problem —
                # it may be one this package never reaches for — and the import that follows is
                # what says whether anything is actually missing.
                try:
                    ctypes.CDLL(library, mode=getattr(ctypes, "RTLD_GLOBAL", 0))
                except OSError:
                    continue
        # Never claims the import. This exists for its side effect; the normal machinery finds
        # the module.
        return None


def _load_screen_module(package_root: str):
    """`frank.screen`, executed on its own, without the package body it lives under.

    Registered in ``sys.modules`` under both names so that everything afterwards — this file, a
    saved workflow, a skill's package — reaches the same module object and therefore the same
    installed bridge. Importing it the ordinary way instead would run `frank/__init__.py`, which
    pulls in the entire runtime for the sake of one file that imports `pathlib` and `typing`.
    """
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
    """What a script starts with: an empty namespace, and everything importable that it may need.

    Deliberately empty. A script reaches the screen the way a program reaches anything —

        from frank.screen import screen

    — and nothing is put into scope for it. The runner used to inject a bound ``screen``, which
    read pleasantly and made the text not a program: it declared none of what it depended on, so
    it could not be saved to a file, could not be read by anyone who did not already know this
    harness, and could not be moved between the inline form and the saved form without being
    rewritten. One of those two was going to have to become the other, and the one with the
    import is the one that is ordinary Python.

    What this does instead is arrange the import path, so that the line above resolves, and so do
    a person's saved workflows and the script packages their skills carry.
    """
    # The path is settled *before* anything is imported, because the import below depends on it.
    # It was written the other way round — `from frank import screen` first, the repair after —
    # so the repair could never run in the one situation it existed for, and every script died
    # with `ModuleNotFoundError: No module named 'frank'`.
    #
    # The child is launched by file path, so Python puts its own directory on `sys.path`, which
    # makes every module beside it importable by a bare name and shadows anything of the same
    # name further along; a sibling called `workflows.py` is exactly that collision. Removing it
    # also removes the only route to `frank` when the package is not installed where this
    # interpreter looks, so the package root goes on explicitly:
    # `.../src/frank/computer` -> `.../src`.
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [entry for entry in sys.path if os.path.abspath(entry or os.getcwd()) != here]
    package_root = os.path.dirname(os.path.dirname(here))
    if package_root not in sys.path:
        sys.path.insert(0, package_root)

    # `frank.screen`, loaded from its file without running `frank/__init__.py`.
    #
    # A plain `from frank import screen` executes the package's `__init__`, and that imports the
    # whole runtime — compaction, credentials, httpx, rich — none of which this process has any
    # use for. It is slow, it is the opposite of the thin disposable child this file exists to
    # be, and under the session's own sandbox it does not even work: `rich` raises
    # `PermissionError` on import, so every script died before its first line with an error
    # naming a library nobody mentioned.
    #
    # So a stub `frank` package is put in `sys.modules` with its real `__path__`, and `screen` is
    # executed into it directly. Anything else under `frank` a script reaches for still imports
    # normally through that path; the package body simply never runs. A workflow or skill saying
    # `from frank.screen import Screen` finds this same module object, which is what makes the
    # inline script and the saved file the same program.
    screen_module = _load_screen_module(package_root)
    screen_module.install_bridge(lambda name, arguments, keywords: _perform(name, arguments, keywords))
    screen_module.screen.target = target
    # A project's and a person's workflow directories, and any script package a skill carries, in
    # precedence order — so `from workflows.invoice import run` reaches whichever wrote it and
    # `from filing import file_all` reaches the skill that ships it. The workflow directories are
    # namespace packages, so Python merges the two into one `workflows` and the project wins a
    # collision.
    for root in reversed(list(workspace or ())):
        if root and root not in sys.path:
            sys.path.insert(0, root)
    # A skill's own dependencies go on the *end*. They have to be reachable — a skill whose
    # package imports `httpx` is broken without them, and the failure names a module the script
    # never mentioned — but they must not be reachable before anything else: one skill pinning an
    # old copy of a library would otherwise decide what every script in this process gets, and
    # `frank` itself is importable from exactly one place for a reason.
    for root in dependencies or ():
        if root and root not in sys.path:
            sys.path.append(root)
    if libraries:
        sys.meta_path.insert(0, _PreloadsBundledLibraries(libraries))
    # The names a surface implements, for a script that wants to ask rather than try. Everything
    # else a script needs, it imports.
    return {"__primitives__": tuple(allowed)}


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
    # Named, so a syntax error reports `File "<control_screen>"` rather than `File "<unknown>"` —
    # the same name the compiles below use, and one that says which script is meant.
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
    """A raised exception as a result: the message once, and the frames that led to it.

    Once, because it used to be twice. ``error`` carried ``f"{type}: {error}"`` and ``traceback``
    carried ``format_exc()``, whose last line is that same string — so a script that raised with a
    payload in the message sent the payload twice. One real script raised with 172 element records
    interpolated into it, and the two fields came to 35,414 and 36,063 characters of the same text.

    The frames stop short of the exception line for exactly that reason: the message is already
    the field above, and a traceback is worth carrying for *where* it happened, not for repeating
    what happened."""
    frames = "".join(traceback.format_tb(error.__traceback__, limit=8)).strip()
    result: dict[str, Any] = {"ok": False, "error": f"{type(error).__name__}: {error}"}
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
        # Building the namespace is inside the guard, because it can fail: it imports
        # `frank.screen` and rewrites `sys.path`, and when it raised, this whole process died
        # before writing anything — so the parent had no result to read and could only say the
        # helper "stopped before it could do anything", which tells a model nothing it can act
        # on. Everything that can go wrong now comes back as an error the script can be fixed by.
        allowed = configuration.get("primitives") or _FALLBACK_PRIMITIVES
        namespace: dict[str, Any] = _script_namespace(allowed, configuration.get("target", ""),
                                                      configuration.get("import_roots", []),
                                                      configuration.get("dependency_roots", []),
                                                      configuration.get("library_roots", []))
        with redirect_stdout(captured):
            result["value"] = _run(script, namespace)
    except SyntaxError as error:
        # The interpreter's own rendering, verbatim — the offending line, the caret under the
        # column, the message. Reporting only `msg` and `lineno` and rewriting them into a
        # sentence threw away the two things that make a syntax error fixable: which text, and
        # where in it. "invalid syntax (line 7)" is not something a program can be repaired
        # from; the caret is. The parent adds its guidance around this rather than instead of it.
        result = {
            "ok": False, "error_code": "syntax_error",
            "detail": error.msg or "", "line": error.lineno or 0,
            "rendered": "".join(traceback.format_exception_only(type(error), error)).strip(),
        }
    except NameError as error:
        # Almost always the one mistake: a script written as though `screen` were in scope,
        # which it was until this became a program that says where its screen comes from.
        # Answered with the line to add rather than with the bare name, because "name 'screen'
        # is not defined" is true and teaches nothing.
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
