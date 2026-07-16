"""Chat routes (split from harness.server.app)."""
from fastapi import APIRouter
from harness.server.app import (
    EventSourceResponse,
    HTTPException,
    PermissionModeRequest,
    PermissionRequest,
    QuestionRequest,
    SteeringRequest,
    _broadcaster,
    _executor_for_context,
    _executors,
    _normalize_permission_mode,
    _pending_permissions,
    _pending_questions,
    _set_session_permission_mode,
    asyncio,
    event,
    json,
)

router = APIRouter()

@router.get("/events")
async def events():
    """Server-sent live events (e.g. agents changed) for the UI to react to."""
    queue = _broadcaster.subscribe()

    async def event_generator():
        try:
            while True:
                event = await queue.get()
                yield {"event": "message", "data": json.dumps(event)}
        finally:
            _broadcaster.unsubscribe(queue)

    return EventSourceResponse(event_generator())


@router.post("/chat/{context_id}/permission")
async def resolve_permission(context_id: str, request: PermissionRequest):
    """Resolve a pending human-in-the-loop permission request. ``deny`` rejects;
    ``allow_once`` and ``allow_always`` both let this command run (``allow_always``
    additionally records a session rule — handled separately)."""
    future = _pending_permissions.get(request.request_id)
    if not future:
        return {"status": "unknown", "error": "No pending permission request with that identifier."}
    if future.done():
        return {"status": "stale", "error": "Permission request was already resolved."}
    # The runtime resumes on the decision string ("deny" / "allow_once" /
    # "allow_always") so it can record a session rule for "allow_always".
    future.set_result(request.decision)
    # The session is no longer waiting — refresh the sidebar marker.
    _broadcaster.publish({"type": "sessions_changed"})
    return {"status": "resolved", "decision": request.decision}


@router.post("/chat/{context_id}/question")
async def resolve_question(context_id: str, request: QuestionRequest):
    """Resolve a pending ask_user request with the user's answers."""
    future = _pending_questions.get(request.request_id)
    if not future:
        return {"status": "unknown", "error": "No pending question with that identifier."}
    if future.done():
        return {"status": "stale", "error": "Question was already resolved."}
    # A dismissal resolves to the decline sentinel the ask_user tool recognizes: it
    # reports the decline to the model and ends the turn, instead of returning
    # answers the user never gave.
    future.set_result({"__declined__": True} if request.declined else request.answers)
    _broadcaster.publish({"type": "sessions_changed"})
    return {"status": "resolved", "answers": request.answers, "declined": request.declined}


@router.post("/chat/{context_id}/steer")
async def steer_context(context_id: str, request: SteeringRequest):
    """Append user steering to an active turn at the next model-call boundary."""
    message = request.message.strip()
    if not message:
        return {"queued": False}
    for executor in _executors.values():
        if executor.steer_context(context_id, message):
            return {"queued": True}
    raise HTTPException(status_code=409, detail="Session is not currently steerable.")


@router.post("/chat/{context_id}/abort")
async def abort_session(context_id: str):
    """Abort the running turn for a context and reject any pending permissions."""
    prefix = f"perm-{context_id}-"
    for request_id, future in list(_pending_permissions.items()):
        if request_id.startswith(prefix) and not future.done():
            future.set_result("deny")
    q_prefix = f"q-{context_id}-"
    for request_id, future in list(_pending_questions.items()):
        if request_id.startswith(q_prefix) and not future.done():
            # A cancelled question resolves to an empty answer list so the
            # awaiting tool call completes cleanly instead of hanging.
            future.set_result([])
    aborted = any(executor.abort_context(context_id) for executor in _executors.values())
    return {"status": "aborted" if aborted else "not_found", "session_id": context_id}


@router.post("/chat/{context_id}/compact")
async def compact_session(context_id: str):
    """Compact a context's conversation on demand (the user pressed compact). Runs a
    background compaction turn that streams its progress and separator to the UI.

    Routes to the session's owning agent resolved from its record, so a session
    reopened after a restart compacts without first needing a fresh message to rebuild
    its runtime — the compaction turn rehydrates it on the way through."""
    executor = await asyncio.to_thread(_executor_for_context, context_id)
    triggered = executor.compact_context(context_id) if executor is not None else False
    if not triggered:
        raise HTTPException(status_code=404, detail="No such session to compact.")
    _broadcaster.publish({"type": "sessions_changed"})
    return {"status": "compacting", "session_id": context_id}


@router.post("/chat/{context_id}/tools/{tool_call_id}/abort")
async def abort_tool_call(context_id: str, tool_call_id: str):
    """Abort one foreground tool call in a running context."""
    aborted = any(executor.abort_tool(context_id, tool_call_id) for executor in _executors.values())
    return {"status": "aborted" if aborted else "not_found", "session_id": context_id, "tool_call_id": tool_call_id}


@router.post("/chat/{context_id}/agents/{task_identifier}/abort")
async def abort_agent(context_id: str, task_identifier: str):
    """Cancel one spawned agent without interrupting its parent turn or peers."""
    aborted = any(executor.cancel_agent(context_id, task_identifier) for executor in _executors.values())
    return {
        "status": "aborted" if aborted else "not_found",
        "session_id": context_id,
        "task_identifier": task_identifier,
    }


@router.post("/chat/{context_id}/tools/{tool_call_id}/background")
async def send_tool_to_background(context_id: str, tool_call_id: str):
    """Push a still-blocking foreground shell command to the background: it keeps
    running detached and the agent's turn continues with a "started" placeholder,
    so the model is notified exactly as if it had backgrounded the command itself."""
    backgrounded = any(executor.send_tool_to_background(context_id, tool_call_id) for executor in _executors.values())
    return {"status": "backgrounded" if backgrounded else "not_found", "session_id": context_id, "tool_call_id": tool_call_id}


@router.get("/chat/{context_id}/background")
async def background_processes(context_id: str):
    """Return live background jobs for the context."""
    jobs: list[dict] = []
    for executor in _executors.values():
        jobs.extend(executor.background_snapshots(context_id))
    return {"session_id": context_id, "jobs": jobs}


@router.post("/chat/{context_id}/permissions/mode")
async def set_permission_mode(context_id: str, request: PermissionModeRequest):
    """Set and persist the permission mode for a context's agent."""
    persisted = await asyncio.to_thread(_set_session_permission_mode, context_id, request.mode)
    if not persisted:
        raise HTTPException(status_code=404, detail="Session not found.")
    updated = any(executor.set_permission_mode(context_id, request.mode) for executor in _executors.values())
    _broadcaster.publish({"type": "sessions_changed"})
    return {"status": "updated" if updated else "saved", "mode": _normalize_permission_mode(request.mode)}
