"""Chat routes."""

from __future__ import annotations
from fastapi import APIRouter
from fastapi import HTTPException
from sse_starlette.sse import EventSourceResponse
import asyncio
import json
from harness.server.models import (
    PermissionModeRequest,
    PermissionRequest,
    QuestionRequest,
    SteeringRequest,
)
from harness.server.state import _broadcaster, _executors
from harness.server.services.sessions import _abort_pending_input, _executor_for_context, _normalize_permission_mode, _resolve_pending_input, _set_session_permission_mode

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
    additionally records a session rule). The decision is recorded against the task's
    durable pending-interaction record and, once the batch is fully answered, the turn
    resumes from its checkpoint — the same path an external ``input_response`` uses."""
    resolved = await _resolve_pending_input(context_id, request.request_id, decision=request.decision)
    if not resolved:
        return {"status": "unknown", "error": "No pending permission request with that identifier."}
    # The session is no longer waiting — refresh the sidebar marker.
    _broadcaster.publish({"type": "sessions_changed"})
    return {"status": "resolved", "decision": request.decision}


@router.post("/chat/{context_id}/question")
async def resolve_question(context_id: str, request: QuestionRequest):
    """Resolve a pending ask_user request with the user's answers (or a dismissal, which
    the ask_user tool reports to the model as a decline). Recorded durably and resumed
    like any other input-required answer."""
    resolved = await _resolve_pending_input(
        context_id, request.request_id, answers=request.answers, declined=request.declined,
    )
    if not resolved:
        return {"status": "unknown", "error": "No pending question with that identifier."}
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
    """Abort the running turn for a context and reject any pending interactions. Aborting
    an input-required task denies its pending gates so the turn resolves (with denials)
    rather than stranding a checkpoint."""
    await _abort_pending_input(context_id)
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
