"""Enforce the package layering, and the two engineering invariants that ride on it.

The architecture's spine is that the daemon never imports the runtime: `daisyd` spawns
worker processes, and the workers carry the heavy runtime (LangChain, LiteLLM, model
clients). Keeping the control plane free of those imports is what lets the warm pool
pre-fork workers cheaply. The second invariant is that `computer` is only ever imported
inside a function: it pulls in PyObjC and CoreFoundation, and a module-level import would
make forking a pre-warmed worker unsafe on macOS.

Neither invariant is visible in a diff, so both are checked mechanically here.

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
    # `rest` is the GUI edge and sits above everything; it serves artifact, terminal, and
    # filesystem features that genuinely need the leaves.
    "rest": {"base", "protocol", "daemon", "locations", "computer", "runtime"},
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
            # PyObjC/CoreFoundation must not be loaded before a worker forks.
            if imported == "computer" and module_level and layer != "computer":
                violations.append(
                    f"{location}: `computer` imported at module level — it must stay "
                    "inside a function so a pre-forked worker never loads PyObjC"
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
