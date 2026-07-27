"""Import every module in the package, so a dangling reference is a failure rather than a
surprise at request time.

`compileall` proves each file parses; it says nothing about whether a name a module reaches
for still exists. This walks the tree and imports each module in isolation, reporting every
failure rather than stopping at the first — a refactor breaks many things at once, and a
checker that reports one of them turns a single pass into a dozen.

Modules that cannot be imported off their intended platform are skipped by name rather than by
swallowing ImportError, so a genuine missing dependency is still a failure.

    python scripts/check_imports.py
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import traceback
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src"
PACKAGE = "daisy"

# `computer` reaches for PyObjC, which exists only on macOS. The layering checker already
# guarantees nothing imports it at module level, so skipping it here does not hide a cycle.
PLATFORM_ONLY_PREFIXES = ("daisy.computer",)


def module_names() -> list[str]:
    package_root = SOURCE_ROOT / PACKAGE
    found = [PACKAGE]
    for module in pkgutil.walk_packages([str(package_root)], prefix=f"{PACKAGE}."):
        found.append(module.name)
    return sorted(found)


def main() -> int:
    sys.path.insert(0, str(SOURCE_ROOT))
    failures: list[tuple[str, str]] = []
    skipped: list[str] = []
    imported = 0

    for name in module_names():
        if sys.platform != "darwin" and name.startswith(PLATFORM_ONLY_PREFIXES):
            skipped.append(name)
            continue
        # An entry point runs a program when imported as `__main__`; importing it by module
        # name is safe, because the guard at the bottom of each one is what starts anything.
        try:
            importlib.import_module(name)
            imported += 1
        except BaseException:  # noqa: BLE001 — every failure is reportable, not just Exception
            failures.append((name, traceback.format_exc()))

    for name, detail in failures:
        print(f"FAIL {name}")
        print("     " + "\n     ".join(detail.strip().splitlines()[-6:]))
    print(f"imported={imported} skipped={len(skipped)} failed={len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
