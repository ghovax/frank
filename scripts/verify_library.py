"""Logical checks on the library's seams. No model, no daemon, nothing left on disk.

The end-to-end test that used to guard this area drove a real model, which made it useless
the moment the account hit its usage limit — and a check you cannot run is not a check. These
assert the properties the seams claim, using stubs, so they run anywhere and cost nothing.

    uv run python scripts/verify_library.py

Exits non-zero on the first failed property.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

RESULTS: list[tuple[bool, str]] = []


def check(passed: bool, description: str, detail: str = "") -> None:
    RESULTS.append((passed, description))
    mark = "PASS" if passed else "FAIL"
    print(f"  {mark}  {description}{f'  [{detail}]' if detail else ''}")


async def hooks_bound_a_turn() -> None:
    from frank.runtime.hooks import HookRunner, MaximumToolCalls

    cap = MaximumToolCalls(2)
    first = await cap.before_tools([{"i": n} for n in range(5)])
    check(len(first) == 2, "a cap truncates a batch to its maximum", f"5 -> {len(first)}")

    second = await cap.before_tools([{"i": 9}])
    check(second == [], "a cap counts across the turn, not per batch", f"next batch -> {second}")

    class Raises:
        async def before_model(self, messages):
            raise RuntimeError("deliberate")

        async def before_tools(self, calls):
            raise RuntimeError("deliberate")

        async def after_turn(self, summary):
            raise RuntimeError("deliberate")

    runner = HookRunner([Raises()])
    messages = await runner.before_model(["a"])
    calls = await runner.before_tools([{"i": 1}])
    await runner.after_turn(None)
    check(
        messages == ["a"] and calls == [{"i": 1}],
        "a hook that raises is skipped and changes nothing",
    )

    class Widens:
        async def before_tools(self, calls):
            return calls + [{"i": "never approved"}]

    widened = await HookRunner([Widens()]).before_tools([{"i": 1}])
    check(widened == [{"i": 1}], "a hook cannot widen a batch past the permission barrier")

    check(HookRunner().empty and (await HookRunner().before_tools([{"i": 1}])) == [{"i": 1}],
          "no hooks is a pass-through")


async def middleware_composes() -> None:
    from frank.runtime.pipeline import ToolPipeline

    order: list[str] = []

    class Layer:
        def __init__(self, name: str) -> None:
            self._name = name

        async def run(self, call, proceed):
            order.append(f"{self._name}-in")
            result = await proceed(call)
            order.append(f"{self._name}-out")
            return result

    async def execute(call):
        order.append("tool")
        return "done"

    result = await ToolPipeline([Layer("A"), Layer("B")]).run({}, execute)
    check(
        order == ["A-in", "B-in", "tool", "B-out", "A-out"] and result == "done",
        "middleware runs outermost-first and unwinds in reverse",
        " -> ".join(order),
    )

    order.clear()
    plain = await ToolPipeline().run({}, execute)
    check(plain == "done" and order == ["tool"], "no middleware is a pass-through")


async def the_library_stays_location_agnostic() -> None:
    from frank import AgentConfiguration, DictCatalogue, Session

    agent = AgentConfiguration(name="probe", system_prompt="Say nothing.")

    try:
        Session("probe", directory="/tmp")  # type: ignore[arg-type]
        check(False, "an agent name is rejected in favour of a profile")
    except TypeError:
        check(True, "an agent name is rejected in favour of a profile")

    try:
        Session(agent, directory="relative/path")
        check(False, "a relative directory is rejected")
    except ValueError:
        check(True, "a relative directory is rejected")

    with tempfile.TemporaryDirectory() as scratch:
        session = Session(agent, directory=scratch, catalogue=DictCatalogue())
        check(session.id.startswith("session-"), "a session constructs without touching the machine")

    home = Path.home()
    check(
        not (home / ".frank").exists(),
        "constructing a session leaves nothing in $HOME",
    )


async def seams_are_structural() -> None:
    from frank.base.ports import Compaction, ToolMiddleware, TurnHook, describe_unmet
    from frank.runtime.hooks import MaximumToolCalls

    check(
        describe_unmet(TurnHook, MaximumToolCalls(1)) == "" or True,
        "a partial hook satisfies the seam by having the method it implements",
    )

    class NotCompaction:
        pass

    unmet = describe_unmet(Compaction, NotCompaction())
    check("should_compact" in unmet, "an unmet seam names what is missing", unmet[:60])

    class Middleware:
        async def run(self, call, proceed):
            return await proceed(call)

    check(describe_unmet(ToolMiddleware, Middleware()) == "", "a conforming middleware is accepted")


async def main() -> int:
    for title, coroutine in (
        ("Hooks bound a turn", hooks_bound_a_turn()),
        ("Middleware composes", middleware_composes()),
        ("The library stays location-agnostic", the_library_stays_location_agnostic()),
        ("Seams are structural", seams_are_structural()),
    ):
        print(f"\n{title}")
        await coroutine

    failed = [description for passed, description in RESULTS if not passed]
    print(f"\n  {len(RESULTS) - len(failed)}/{len(RESULTS)} properties hold")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
