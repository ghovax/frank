"""Unified-durable-turn store layer: the conversation checkpoint, the data-driven
restart reconciliation, and context deletion — exercised against a real in-memory SQLite.
"""
import uuid

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from a2a.types import Task, TaskStatus, TaskState, Message, Role, Part, TextPart
from harness.core.task_store import (
    AppendOnlyTaskStore,
    TURN_KIND_KEY,
    TURN_KIND_USER,
    TURN_KIND_DELEGATED,
)


async def _store() -> AppendOnlyTaskStore:
    store = AppendOnlyTaskStore(create_async_engine("sqlite+aiosqlite:///:memory:"))
    await store.initialize()
    return store


def _task(task_id: str, context_id: str, state: TaskState, metadata=None) -> Task:
    return Task(
        id=task_id,
        context_id=context_id,
        kind="task",
        status=TaskStatus(state=state),
        history=[Message(
            role=Role.user, parts=[Part(root=TextPart(text="hi"))],
            message_id=uuid.uuid4().hex, task_id=task_id, context_id=context_id,
        )],
        metadata=metadata,
    )


async def test_checkpoint_roundtrip_and_compaction_replace():
    store = await _store()
    ctx = "ctx"
    first = [{"type": "human", "data": {"content": "hello"}},
             {"type": "ai", "data": {"content": "hi there"}}]
    await store.save_checkpoint(ctx, "t1", first)
    assert await store.load_checkpoint(ctx) == first

    # Compaction rewrites the whole conversation in place — the snapshot replaces,
    # it does not append (the reason it is a whole snapshot, not a per-turn log).
    compacted = [{"type": "system", "data": {"content": "summary of earlier"}},
                 {"type": "human", "data": {"content": "next"}}]
    await store.save_checkpoint(ctx, "t2", compacted)
    assert await store.load_checkpoint(ctx) == compacted

    assert await store.load_checkpoint("missing") == []


async def test_reconciliation_policy():
    store = await _store()
    ctx = "ctx"
    await store.save(_task("top-ir", ctx, TaskState.input_required, {TURN_KIND_KEY: TURN_KIND_USER}))
    await store.save(_task("deleg-ir", ctx, TaskState.input_required, {TURN_KIND_KEY: TURN_KIND_DELEGATED}))
    await store.save(_task("running", ctx, TaskState.working, {TURN_KIND_KEY: TURN_KIND_USER}))
    await store.save(_task("done", ctx, TaskState.completed, {TURN_KIND_KEY: TURN_KIND_USER}))
    await store.save(_task("legacy-ir", ctx, TaskState.input_required, None))  # unmarked

    failed = set(await store.reconcile_orphaned_turns())
    assert failed == {"deleg-ir", "running"}

    assert (await store.get("top-ir")).status.state == TaskState.input_required
    assert (await store.get("legacy-ir")).status.state == TaskState.input_required
    assert (await store.get("deleg-ir")).status.state == TaskState.failed
    assert (await store.get("running")).status.state == TaskState.failed
    assert (await store.get("done")).status.state == TaskState.completed


async def test_delete_context_wipes_tasks_and_checkpoint():
    store = await _store()
    ctx = "ctx"
    await store.save(_task("t", ctx, TaskState.completed))
    await store.save_checkpoint(ctx, "t", [{"type": "human", "data": {"content": "x"}}])
    await store.delete_context(ctx)
    assert await store.get("t") is None
    assert await store.load_checkpoint(ctx) == []
