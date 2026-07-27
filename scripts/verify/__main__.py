"""Run the verification battery.

    uv run python -m scripts.verify              # everything, in dependency order
    uv run python -m scripts.verify prototype    # one stage
    uv run python -m scripts.verify --list       # what there is

Exit status is the number of failed stages, so a shell can act on it. A skipped stage is not a
failure: some claims need a kernel feature or a tool this machine may not have, and reporting
that honestly is more useful than a red mark nobody can act on.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from scripts.verify.harness import Outcome, report
from scripts.verify.stages import STAGES


def _run(name: str) -> Outcome:
    started = time.monotonic()
    report("stage_started", stage=name)
    try:
        outcome = STAGES[name]()
    except Exception as error:  # noqa: BLE001 — one stage failing must not end the battery
        logging.getLogger("verify").exception("stage %s raised", name)
        outcome = Outcome(name, False, reason=f"{type(error).__name__}: {error}")
    report(
        "stage_finished",
        stage=name,
        passed=outcome.passed,
        skipped=outcome.skipped,
        seconds=round(time.monotonic() - started, 2),
        reason=outcome.reason,
        observations=outcome.observations,
    )
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.verify", description=__doc__)
    parser.add_argument("stages", nargs="*", help="stages to run (default: all)")
    parser.add_argument("--list", action="store_true", help="list the stages and exit")
    arguments = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if arguments.list:
        for name in STAGES:
            print(name)
        return 0

    unknown = [name for name in arguments.stages if name not in STAGES]
    if unknown:
        parser.error(f"unknown stage(s): {', '.join(unknown)}. Known: {', '.join(STAGES)}")

    selected = arguments.stages or list(STAGES)
    outcomes = [_run(name) for name in selected]

    failed = [outcome for outcome in outcomes if not outcome.passed]
    skipped = [outcome for outcome in outcomes if outcome.skipped]
    report(
        "battery_finished",
        total=len(outcomes),
        passed=len(outcomes) - len(failed),
        failed=[outcome.name for outcome in failed],
        skipped=[outcome.name for outcome in skipped],
    )

    print(file=sys.stderr)
    for outcome in outcomes:
        mark = "SKIP" if outcome.skipped else ("PASS" if outcome.passed else "FAIL")
        detail = f"  ({outcome.reason})" if outcome.reason else ""
        print(f"  {mark}  {outcome.name}{detail}", file=sys.stderr)
    print(file=sys.stderr)

    return len(failed)


if __name__ == "__main__":
    sys.exit(main())
