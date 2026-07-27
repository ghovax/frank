"""Enforce the package layering, and the engineering invariants that ride on it.

The architecture's spine is that the daemon never imports the runtime: `daisyd` spawns
worker processes, and the workers carry the heavy runtime (LangChain, LiteLLM, model
clients). Keeping the control plane free of those imports is what lets the warm pool
pre-fork workers cheaply. The second invariant is that `computer` is only ever imported
inside a function: it pulls in PyObjC and CoreFoundation, and a module-level import would
make forking a pre-warmed worker unsafe on macOS.

Two more invariants join them. Nothing under `runtime` may take session state from a caller
and park it in a module global — the setter pattern — because the runtime is a library: an
embedder imports it, and one process may host more than one session, so the last caller would
win and everyone else would silently get their neighbour's configuration. That was not
theoretical. The confinement profile lived in a module global, and a `bash` call naming its own
working directory rewrote it mid-turn, narrowing every other open turn's sandbox in the same
process. Session state belongs on the runtime or in a context variable. Memoization is
untouched by the rule: a cache written by the function that reads it, from something it
computed, is shared harmlessly.

And no module outside a composition root may install a signal handler or an exit hook at
import. Importing a library must not seize the host program's signals, and the tool registry
did exactly that — which also killed a forked child the instant it was signalled.

None of these invariants is visible in a diff, so all of them are checked mechanically here.

One exemption, and only one: a package's `__main__.py` is its composition root. Assembling a
program is precisely the act of reaching across layers — the daemon's entry point serves the
GUI surface that sits above it — and forbidding that would only push the wiring into a module
that has no business knowing about it. The layer table constrains what the *parts* may know
about each other; the entry point is where they are put together. The `computer` invariant
still applies there, because that one is about what a process has loaded, not about who knows
about whom.

    python scripts/check_layers.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "daisy"

# What each layer may import. A layer may always import itself.
ALLOWED: dict[str, set[str]] = {
    "base": set(),
    "protocol": {"base"},
    "computer": {"base"},
    "locations": {"base"},
    "runtime": {"base", "protocol", "computer", "locations"},
    "worker": {"base", "protocol", "runtime"},
    # The daemon may reach the location value types — they are leaf dataclasses describing
    # where work runs, not runtime machinery. What it must never reach is `runtime`: that is
    # the import cost the pre-forked worker exists to carry.
    "daemon": {"base", "protocol", "locations"},
    "cli": {"base", "protocol"},
    # `rest` is the browser edge and sits above everything. It does not reach `runtime`: the
    # account state the settings surface needs — the subscription catalogue and the usage
    # snapshot — lives in `base`, beside the credentials it describes.
    "rest": {"base", "protocol", "daemon", "locations", "computer"},
}


def _layer_of(path: Path, source_root: Path) -> str | None:
    relative = path.relative_to(source_root)
    return relative.parts[0] if len(relative.parts) > 1 else None


def _imported_layers(tree: ast.AST) -> list[tuple[str, int, bool]]:
    """Every `daisy.<layer>` import: the layer, its line, and whether it is module level."""
    found: list[tuple[str, int, bool]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.depth = 0

        def _record(self, module: str, line: int) -> None:
            parts = module.split(".")
            if len(parts) >= 2 and parts[0] == PACKAGE:
                found.append((parts[1], line, self.depth == 0))

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                self._record(alias.name, node.lineno)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.module and node.level == 0:
                self._record(node.module, node.lineno)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    Visitor().visit(tree)
    return found


# Calls that configure the whole process. A library performs none of them at import.
_PROCESS_WIDE_CALLS = {
    ("signal", "signal"): "installs a process-wide signal handler",
    ("atexit", "register"): "registers a process-wide exit hook",
}


def _injected_module_state(tree: ast.AST) -> list[tuple[str, int]]:
    """Functions that store an argument into a module global — the setter pattern.

    The distinction this draws is between *injection* and *memoization*, and it is the whole
    reason the check is worth having. A memo is written by the function that reads it, from
    something it computed or fetched, and two sessions sharing one is harmless. A setter takes
    a value from its caller and parks it at module scope, which means the last caller wins and
    every other session in the process silently gets their neighbour's configuration.

    Eight of these installed the confinement profile, the search client, the MCP manager, the
    web-fetch clients and the session's own identity. They held only because a worker served
    exactly one session, and they stopped holding the moment anything else imported the
    runtime.
    """
    found: list[tuple[str, int]] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        declared: set[str] = set()
        for inner in ast.walk(node):
            if isinstance(inner, ast.Global):
                declared.update(inner.names)
        if not declared:
            continue
        parameters = {
            argument.arg
            for group in (node.args.posonlyargs, node.args.args, node.args.kwonlyargs)
            for argument in group
        }
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Assign):
                continue
            assigned = {
                target.id for target in inner.targets if isinstance(target, ast.Name)
            } & declared
            if not assigned:
                continue
            # The value is a parameter, or trivially derived from one (`value or ""`).
            sources = {
                sub.id for sub in ast.walk(inner.value) if isinstance(sub, ast.Name)
            }
            if sources & parameters:
                name = sorted(assigned)[0]
                found.append((
                    f"`{node.name}` stores its argument into the module global `{name}`",
                    inner.lineno,
                ))
    return found


def _process_wide_configuration(tree: ast.AST) -> list[tuple[str, int]]:
    """Import-time calls that configure the whole process."""
    found: list[tuple[str, int]] = []
    for node in tree.body:
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            parts = ast.unparse(inner.func).split(".")
            if len(parts) == 2 and (parts[0], parts[1]) in _PROCESS_WIDE_CALLS:
                found.append((_PROCESS_WIDE_CALLS[(parts[0], parts[1])], inner.lineno))
    return found


def main() -> int:
    source_root = ROOT / "src" / PACKAGE
    if not source_root.is_dir():
        print(f"no {source_root} yet — nothing to check")
        return 0

    violations: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        layer = _layer_of(path, source_root)
        if layer is None:
            continue
        composition_root = path.name == "__main__.py"
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as error:
            violations.append(f"{path.relative_to(ROOT)}: could not parse ({error})")
            continue
        allowed = ALLOWED.get(layer)
        if allowed is None:
            continue
        for imported, line, module_level in _imported_layers(tree):
            location = f"{path.relative_to(ROOT)}:{line}"
            if not composition_root and imported != layer and imported not in allowed:
                violations.append(f"{location}: {layer} may not import {imported}")
            # PyObjC initialises CoreFoundation, and a prototype that has done so cannot be
            # forked safely on macOS.
            if imported == "computer" and module_level and layer != "computer":
                violations.append(
                    f"{location}: `computer` imported at module level — it must stay "
                    "inside a function so the forking prototype never initialises CoreFoundation"
                )

        # The runtime is a library: an embedder imports it, and one process may host more
        # than one session, so nothing in it may take session state from a caller and park it
        # at module scope.
        if layer == "runtime":
            for description, line in _injected_module_state(tree):
                violations.append(
                    f"{path.relative_to(ROOT)}:{line}: {description} — session state belongs "
                    "on the runtime or in a context variable"
                )

        # No layer configures the whole process at import. A composition root may, because
        # that is what a program's entry point is for.
        if not composition_root:
            for description, line in _process_wide_configuration(tree):
                violations.append(
                    f"{path.relative_to(ROOT)}:{line}: {description} at import — "
                    "only a composition root may configure the process"
                )

    if violations:
        print(f"{len(violations)} layering violation(s):\n")
        for violation in violations:
            print(f"  {violation}")
        return 1
    print("layering ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
