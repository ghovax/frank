"""The two job stores must be interchangeable, because the runtime picks one at construction.

`JobStore` is a `@runtime_checkable` Protocol, and that check compares method *names* only —
never their signatures. So when the protocol and its in-memory default were written against a
`request=` parameter while the only caller passed `arguments=` and `tool_call_id=`, nothing
complained at wiring time. It surfaced as a `TypeError` on the first background job of every
session, and because `bash`, `search_web`, `fetch_url` and `download_file` all route through
`BackgroundJobs.spawn`, that meant every one of those tools failed in every daemon session.

The model saw only `MemoryJobStore.record_started() got an unexpected keyword argument
'arguments'`, could not interpret it, and told the user its command had been "blocked by the
local command runner" — a plausible sentence about a thing that never happened.

These tests exercise the port the way the runtime does, rather than asserting on signatures, so
they keep holding if the parameters are renamed together.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from frank.base.background_store import BackgroundJobStore
from frank.base.ports import JobStore, MemoryJobStore
from frank.runtime.background import BackgroundJobs


def _stores(tmp_path):
    """Every implementation the runtime might be handed, built for a test."""
    return {
        "MemoryJobStore": MemoryJobStore(),
        "BackgroundJobStore": BackgroundJobStore(database_path=tmp_path / "jobs.db"),
    }


@pytest.mark.parametrize("store_name", ["MemoryJobStore", "BackgroundJobStore"])
def test_a_job_can_be_recorded_through_the_runtime(tmp_path, store_name):
    """The call the runtime actually makes, against each store.

    Driven through `BackgroundJobs.spawn` rather than by calling `record_started` directly,
    because the defect was a disagreement between caller and implementation — testing the
    implementation alone would have reproduced the disagreement rather than caught it."""
    store = _stores(tmp_path)[store_name]

    async def run() -> None:
        async def work() -> str:
            return "done"

        jobs = BackgroundJobs(session_id="session-1", agent_name="agent", store=store)
        jobs.spawn("bash", work(), arguments={"command": "ls -la"}, tool_call_identifier="call-1")
        await asyncio.sleep(0.05)

    asyncio.run(run())
    recorded = store.undelivered_jobs("session-1", "agent") or store.running_jobs("agent")
    assert recorded, f"{store_name} recorded no job at all"


def test_both_stores_agree_on_the_shape_of_a_job(tmp_path):
    """A reader of a job row must not be able to tell which store produced it.

    The in-memory store once wrote a `request` key where SQLite wrote `arguments`, which would
    have made every consumer correct against one store and wrong against the other."""
    stores = _stores(tmp_path)

    async def record(store) -> None:
        async def work() -> str:
            return "done"

        jobs = BackgroundJobs(session_id="session-1", agent_name="agent", store=store)
        jobs.spawn("bash", work(), arguments={"command": "ls"}, tool_call_identifier="call-1")
        await asyncio.sleep(0.05)

    shapes = {}
    for name, store in stores.items():
        asyncio.run(record(store))
        rows = store.undelivered_jobs("session-1", "agent") or store.running_jobs("agent")
        assert rows, f"{name} recorded no job"
        shapes[name] = set(rows[0].keys())

    # Timestamps are the durable store's own business — it is the one restarts happen to, and
    # nothing reads them off a row. What must agree is the job's identity and payload, which is
    # what a consumer actually looks at.
    identifying = {"job_id", "session_id", "agent_name", "kind", "arguments", "tool_call_id",
                   "status", "result", "process_group"}
    missing = (shapes["BackgroundJobStore"] & identifying) - shapes["MemoryJobStore"]
    assert not missing, (
        f"the in-memory store omits keys the durable one provides: {sorted(missing)} — a reader "
        f"of a job row would behave differently depending on which store the runtime was given"
    )


def test_the_protocol_matches_its_implementations(tmp_path):
    """`@runtime_checkable` compares names, not signatures — so this compares the signatures.

    Without it, an implementation can satisfy `isinstance(store, JobStore)` while being
    uncallable, which is exactly the state this port shipped in."""
    for name, store in _stores(tmp_path).items():
        assert isinstance(store, JobStore), f"{name} does not satisfy the protocol"
        for method_name in ("record_started", "record_finished", "mark_delivered"):
            expected = inspect.signature(getattr(JobStore, method_name))
            actual = inspect.signature(getattr(type(store), method_name))
            assert set(expected.parameters) == set(actual.parameters), (
                f"{name}.{method_name} takes {sorted(actual.parameters)} but the protocol "
                f"declares {sorted(expected.parameters)}; isinstance() cannot see this"
            )
