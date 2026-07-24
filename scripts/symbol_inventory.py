"""Record every module's public symbols, so a restructure can prove it dropped nothing.

The migration slices large modules across new packages; a helper can silently vanish in
the process. Capturing the inventory *before* anything moves gives the end-state
verification something to diff against: every public symbol must still exist somewhere,
or appear on the deletion list.

    python scripts/symbol_inventory.py --write baseline.json     # before the move
    python scripts/symbol_inventory.py --compare baseline.json   # after it
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _public_symbols(path: Path) -> list[str]:
    """Top-level classes, functions, and assigned names that are not private."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return sorted(name for name in names if not name.startswith("__"))


def _inventory(source_root: Path) -> dict[str, list[str]]:
    inventory: dict[str, list[str]] = {}
    for path in sorted(source_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        inventory[str(path.relative_to(ROOT))] = _public_symbols(path)
    return inventory


def _source_root() -> Path:
    for candidate in ("src/xeac", "src/daisy"):
        path = ROOT / candidate
        if path.is_dir():
            return path
    raise SystemExit("no source package found under src/")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", metavar="PATH", help="write the inventory as JSON")
    parser.add_argument("--compare", metavar="PATH", help="compare against a written inventory")
    arguments = parser.parse_args()

    inventory = _inventory(_source_root())

    if arguments.write:
        Path(arguments.write).write_text(json.dumps(inventory, indent=2) + "\n")
        total = sum(len(names) for names in inventory.values())
        print(f"wrote {len(inventory)} modules, {total} public symbols")
        return 0

    if arguments.compare:
        baseline = json.loads(Path(arguments.compare).read_text())
        before = {name for names in baseline.values() for name in names}
        after = {name for names in inventory.values() for name in names}
        missing = sorted(before - after)
        print(f"baseline {len(before)} symbols, current {len(after)} symbols")
        if missing:
            print(f"\n{len(missing)} symbol(s) no longer present anywhere:")
            for name in missing:
                origin = sorted(module for module, names in baseline.items() if name in names)
                print(f"  {name}  (was in {', '.join(origin)})")
        return 0

    total = sum(len(names) for names in inventory.values())
    print(f"{len(inventory)} modules, {total} public symbols")
    return 0


if __name__ == "__main__":
    sys.exit(main())
