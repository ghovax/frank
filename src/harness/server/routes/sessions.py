"""Sessions routes."""
from fastapi import APIRouter
from harness.server.database import SessionRecord
from fastapi import Request
from harness.core.configuration import harness_home_directory
from sse_starlette.sse import EventSourceResponse
import asyncio
import json
import re
from harness.server.models import (
    SessionDraftRequest,
)
from harness.server import state
from harness.server.services.broadcast import _publish_broadcast
from harness.server.services.artifacts import _prune_session_artifacts
from harness.server.services.sessions import _abort_pending_input, _remove_upload_file, _session_draft, _sessions_payload, _update_session_draft

router = APIRouter()

@router.get("/sessions")
async def list_sessions():
    return await asyncio.to_thread(_sessions_payload)


@router.get("/sessions/{context_id}/draft")
async def session_draft(context_id: str):
    return {"input_draft": await asyncio.to_thread(_session_draft, context_id)}


@router.put("/sessions/{context_id}/draft")
async def update_session_draft(context_id: str, request: SessionDraftRequest):
    await asyncio.to_thread(_update_session_draft, context_id, request.input_draft)
    return {"ok": True}


@router.get("/sessions/{context_id}/tasks")
async def session_tasks(context_id: str):
    """All A2A tasks for a context — the main turn tasks (with history and
    artifacts) plus related agent tasks — for replaying a session."""
    assert state._task_store is not None
    tasks = await state._task_store.tasks_for_context(context_id)
    return {
        "tasks": [
            task.model_dump(by_alias=True, exclude_none=True, mode="json")
            for task in tasks
        ]
    }


@router.get("/sessions/{context_id}/tasks/page")
async def session_task_page(context_id: str, before_row_id: int | None = None, limit: int = 400):
    """A bounded replay page for fast session switching.

    Returns the newest persisted task-history rows first on the initial call;
    pass ``before_row_id`` from the previous response to load older rows. This
    keeps long conversations interactive without waiting for the complete task
    history to deserialize and cross the local HTTP boundary.
    """
    assert state._task_store is not None
    page = await state._task_store.task_page_for_context(context_id, before_row_id=before_row_id, limit=limit)
    return {
        "tasks": [
            task.model_dump(by_alias=True, exclude_none=True, mode="json")
            for task in page["tasks"]
        ],
        "next_before_row_id": page["next_before_row_id"],
        "has_more": page["has_more"],
    }


@router.get("/sessions/{context_id}/stream")
async def session_stream(context_id: str, request: Request):
    """Live SSE stream of a session's structured parts for a non-driving viewer.

    Emits one ``snapshot`` frame (the compacted transcript, same shape as
    /sessions/{id}/tasks) then a ``live`` tail — one frame per part the turn emits,
    in the same agent-message shape the driver's message/stream uses, so the client
    feeds them to the same reducer. Replaces per-second polling + full re-replay
    (O(N)/s) with O(delta) live updates.

    A ``done`` frame ends the stream when the turn completes (or if it already had
    by the time the viewer connected)."""
    assert state._task_store is not None
    task_store = state._task_store

    async def generate():
        # Subscribe before reading the baseline so every part published from here on
        # lands on our queue; the snapshot then covers everything up to the baseline.
        queue = state._event_bus.subscribe(context_id)
        baseline = state._event_bus.high_seq(context_id)
        try:
            tasks = await task_store.tasks_for_context(context_id)
            yield {"data": json.dumps({
                "kind": "snapshot",
                "tasks": [task.model_dump(by_alias=True, exclude_none=True, mode="json") for task in tasks],
            })}

            for sequence, part in state._event_bus.agent_events_through(context_id, baseline):
                yield {"data": json.dumps({
                    "kind": "live",
                    "seq": sequence,
                    "message": {"role": "agent", "parts": [part]},
                })}

            # Drain anything queued between subscribe and now. Events with seq <=
            # baseline are already in the snapshot; only newer ones are sent live.
            done = False
            while not queue.empty():
                item = queue.get_nowait()
                if item is state.ContextEventBus._DONE:
                    done = True
                    break
                seq, part = item
                if seq <= baseline:
                    continue
                yield {"data": json.dumps({"kind": "live", "seq": seq, "message": {"role": "agent", "parts": [part]}})}

            if done or state._running_contexts.get(context_id, 0) == 0:
                yield {"data": json.dumps({"kind": "done"})}
                return

            # Live tail. The wait_for timeout lets us notice a client disconnect
            # promptly; the library's ping keeps the connection alive between events.
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                if item is state.ContextEventBus._DONE:
                    yield {"data": json.dumps({"kind": "done"})}
                    break
                seq, part = item
                yield {"data": json.dumps({"kind": "live", "seq": seq, "message": {"role": "agent", "parts": [part]}})}
        except asyncio.CancelledError:
            raise
        finally:
            state._event_bus.unsubscribe(context_id, queue)

    return EventSourceResponse(generate(), ping=15)


@router.delete("/sessions/{context_id}")
async def delete_session(context_id: str):
    """Permanently delete a session and all its tasks. Aborts the context first."""
    # Settle any input-required pause and drop the awaiting-input marker; the pending
    # record and tasks are removed with the session below regardless.
    await _abort_pending_input(context_id)
    state._awaiting_input_contexts.discard(context_id)
    # Release every executor's live state for this context (runtime, resume pump, turn
    # lock, flags, and the shared conversation) so a deleted session leaves nothing
    # behind. Teardown subsumes abort — it stops any in-flight turn and pump first.
    for executor in state._executors.values():
        executor.teardown_context(context_id)
    # Delete every task in the context from the task store, then reclaim any upload files
    # the session referenced that no surviving session still references (uploads are
    # content-addressed and may be shared, so only truly-orphaned files are removed).
    if state._task_store is not None:
        uploads_root = str(harness_home_directory() / "uploads")
        upload_pattern = re.compile(re.escape(uploads_root) + r"/[^\"\\]+")
        referenced_uploads: set[str] = set()
        for text in await state._task_store.context_message_texts(context_id):
            referenced_uploads.update(upload_pattern.findall(text))
        # One call drops the context's tasks (head/history/artifacts) and its
        # conversation checkpoint — the single durable turn surface.
        await state._task_store.delete_context(context_id)
        for path_string in referenced_uploads:
            if not await state._task_store.any_history_references(path_string):
                await asyncio.to_thread(_remove_upload_file, path_string, uploads_root)
    # Prune this session's artifact versions (shadow-git branches + index rows) before the
    # session record goes, so its locations can still be resolved for the branch delete.
    await asyncio.to_thread(_prune_session_artifacts, context_id)
    # The retention pass above already removed persisted conversation/lifecycle state;
    # finish by removing the sidebar record. teardown_context dropped the live copies.
    def _delete_record() -> bool:
        assert state._session_factory is not None
        database_session = state._session_factory()
        try:
            record = database_session.query(SessionRecord).filter(SessionRecord.id == context_id).first()
            if record is None:
                database_session.commit()
                return False
            database_session.delete(record)
            database_session.commit()
            return True
        finally:
            database_session.close()
    deleted = await asyncio.to_thread(_delete_record)
    _publish_broadcast({"type": "sessions_changed"})
    return {"status": "deleted" if deleted else "not_found", "session_id": context_id}
