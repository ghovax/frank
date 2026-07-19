#!/usr/bin/env python3
"""Guard the typed-turn-core invariants that have landed, so the entropy they removed does not
creep back. Each check corresponds to a boundary the refactor made typed; a violation is a
regression toward the untyped-dictionary / bare-string style the plan retired.

Run: ``python scripts/check_typed_boundaries.py`` (exit non-zero on any violation). Intended for CI.

Scope note: this guards only the boundaries that are already fully typed (the runtime event
contract, the permission mode, and the turn-metadata control-state). The DataPart wire vocabulary
and the frontend reducers are not yet unified onto one catalog, so they are deliberately not guarded
here — adding them is part of that later, cross-cutting change.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "src" / "harness" / "core"

# Files permitted to define/own each typed primitive (and therefore to mention the raw form).
TURN_EVENTS = CORE / "turn_events.py"
TOOL_POLICY = CORE / "tool_policy.py"
TURN_RECORD = CORE / "turn_record.py"


def _violations() -> list[str]:
    problems: list[str] = []

    # 1. The runtime->executor event contract is the typed union; the legacy StreamEvent carrier
    #    is gone. No module may reintroduce it. (turn_events.py documents the retired name.)
    for path in CORE.glob("*.py"):
        if path == TURN_EVENTS:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\bStreamEvent\b", line):
                problems.append(f"{path.relative_to(ROOT)}:{lineno}: `StreamEvent` is retired — use the typed union in turn_events.py")

    # 2. Permission modes are the PermissionMode enum, not bare strings compared inline. Flag
    #    equality against a mode literal outside the enum's own module.
    mode_compare = re.compile(r'[=!]=\s*"(default|auto|read_only|bypass)"|"(default|auto|read_only|bypass)"\s*[=!]=')
    for path in CORE.glob("*.py"):
        if path == TOOL_POLICY:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if mode_compare.search(line):
                problems.append(f"{path.relative_to(ROOT)}:{lineno}: bare permission-mode string compare — use PermissionMode")

    # 3. A turn's durable control-state is read through TurnRecord, never by indexing the raw
    #    task metadata for its keys.
    raw_meta = re.compile(r'metadata[^\n]*(?:\.get\(\s*|\[\s*)"(pendingInteraction|daisyTurnKind|referenceTaskIds|agentLane)"')
    for path in CORE.glob("*.py"):
        if path == TURN_RECORD:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if raw_meta.search(line):
                problems.append(f"{path.relative_to(ROOT)}:{lineno}: raw turn-metadata key access — use TurnRecord.from_metadata(...)")

    return problems


def main() -> int:
    problems = _violations()
    if problems:
        print("Typed-boundary violations (typed-turn-core invariants):\n")
        for problem in problems:
            print(f"  {problem}")
        print(f"\n{len(problems)} violation(s).")
        return 1
    print("Typed-boundary invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
