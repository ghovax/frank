"""Sessions routes (split from harness.server.app)."""
from fastapi import APIRouter
from harness.server import app as _app
from harness.server.app import (
    EventSourceResponse,
    Request,
    SessionDraftRequest,
    SessionRecord,
    _ContextEventBus,
    _event_bus,
    _executors,
    _pending_permissions,
    _pending_questions,
    _prune_session_artifacts,
    _publish_broadcast,
    _remove_upload_file,
    _running_contexts,
    _session_draft,
    _sessions_payload,
    _update_session_draft,
    asyncio,
    harness_home_directory,
    json,
    re,
    text,
)

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
    assert _app._task_store is not None
    tasks = await _app._task_store.tasks_for_context(context_id)
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
    assert _app._task_store is not None
    page = await _app._task_store.task_page_for_context(context_id, before_row_id=before_row_id, limit=limit)
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
    assert _app._task_store is not None
    task_store = _app._task_store

    async def generate():
        # Subscribe before reading the baseline so every part published from here on
        # lands on our queue; the snapshot then covers everything up to the baseline.
        queue = _event_bus.subscribe(context_id)
        baseline = _event_bus.high_seq(context_id)
        try:
            tasks = await task_store.tasks_for_context(context_id)
            yield {"data": json.dumps({
                "kind": "snapshot",
                "tasks": [task.model_dump(by_alias=True, exclude_none=True, mode="json") for task in tasks],
            })}

            # Drain anything queued between subscribe and now. Events with seq <=
            # baseline are already in the snapshot; only newer ones are sent live.
            done = False
            while not queue.empty():
                item = queue.get_nowait()
                if item is _ContextEventBus._DONE:
                    done = True
                    break
                seq, part = item
                if seq <= baseline:
                    continue
                yield {"data": json.dumps({"kind": "live", "seq": seq, "message": {"role": "agent", "parts": [part]}})}

            if done or _running_contexts.get(context_id, 0) == 0:
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
                if item is _ContextEventBus._DONE:
                    yield {"data": json.dumps({"kind": "done"})}
                    break
                seq, part = item
                yield {"data": json.dumps({"kind": "live", "seq": seq, "message": {"role": "agent", "parts": [part]}})}
        except asyncio.CancelledError:
            raise
        finally:
            _event_bus.unsubscribe(context_id, queue)

    return EventSourceResponse(generate(), ping=15)


@router.delete("/sessions/{context_id}")
async def delete_session(context_id: str):
    """Permanently delete a session and all its tasks. Aborts the context first."""
    # Abort any running turn and settle pending prompts.
    for request_id, future in list(_pending_permissions.items()):
        if request_id.startswith(f"perm-{context_id}-") and not future.done():
            future.set_result("deny")
    q_prefix = f"q-{context_id}-"
    for request_id, future in list(_pending_questions.items()):
        if request_id.startswith(q_prefix) and not future.done():
            future.set_result([])
    for executor in _executors.values():
        executor.abort_context(context_id)
    # Delete every task in the context from the task store, then reclaim any upload files
    # the session referenced that no surviving session still references (uploads are
    # content-addressed and may be shared, so only truly-orphaned files are removed).
    if _app._task_store is not None:
        uploads_root = str(harness_home_directory() / "uploads")
        upload_pattern = re.compile(re.escape(uploads_root) + r"/[^\"\\]+")
        referenced_uploads: set[str] = set()
        for text in await _app._task_store.context_message_texts(context_id):
            referenced_uploads.update(upload_pattern.findall(text))
        task_ids = await _app._task_store.task_ids_for_context(context_id)
        for task_id in task_ids:
            await _app._task_store.delete(task_id)
        for path_string in referenced_uploads:
            if not await _app._task_store.any_history_references(path_string):
                await asyncio.to_thread(_remove_upload_file, path_string, uploads_root)
    # Prune this session's artifact versions (shadow-git branches + index rows) before the
    # session record goes, so its locations can still be resolved for the branch delete.
    await asyncio.to_thread(_prune_session_artifacts, context_id)
    # Delete the session record from the sessions table.
    def _delete_record() -> bool:
        assert _app._session_factory is not None
        database_session = _app._session_factory()
        try:
            record = database_session.query(SessionRecord).filter(SessionRecord.id == context_id).first()
            if record is None:
                return False
            database_session.delete(record)
            database_session.commit()
            return True
        finally:
            database_session.close()
    deleted = await asyncio.to_thread(_delete_record)
    _publish_broadcast({"type": "sessions_changed"})
    return {"status": "deleted" if deleted else "not_found", "session_id": context_id}
