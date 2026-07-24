"""Where workers' writes land — the sole-writer intake.

Workers never open the database. They send their persistence calls here, and this module
performs them against the one store the daemon owns. That keeps the append-only store's
single-writer property intact and keeps every write ordered, which is what the row-ordered
history depends on.

Live turn events arrive on the same channel and are fanned out to whoever is attached, so an
event reaches a watcher at the moment it is persisted rather than on some later poll.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from a2a.types import Task

from xeac.daemon import state

logger = logging.getLogger(__name__)

router = APIRouter()

# Per-session frame counter for the live stream.
_SEQUENCE: dict[str, int] = {}


async def _task_save(params: dict) -> dict:
    task = Task.model_validate(params.get("task") or {})
    await state.task_store.save(task)
    return {"saved": task.id}


async def _task_get(params: dict) -> dict:
    task = await state.task_store.get(str(params.get("task_id") or ""))
    return task.model_dump(by_alias=True, exclude_none=True, mode="json") if task else None


async def _task_delete(params: dict) -> dict:
    await state.task_store.delete(str(params.get("task_id") or ""))
    return {"deleted": True}


async def _turn_save_state(params: dict) -> dict:
    await state.task_store.save_turn_state(
        str(params.get("context_id") or ""),
        str(params.get("task_id") or ""),
        params.get("messages") or [],
        params.get("session_state"),
    )
    return {"saved": True}


async def _turn_load_checkpoint(params: dict) -> Any:
    return await state.task_store.load_checkpoint(str(params.get("context_id") or ""))


async def _turn_load_session_state(params: dict) -> Any:
    return await state.task_store.load_session_state(str(params.get("context_id") or ""))


async def _turn_tasks_for_context(params: dict) -> Any:
    tasks = await state.task_store.tasks_for_context(str(params.get("context_id") or ""))
    return [task.model_dump(by_alias=True, exclude_none=True, mode="json") for task in tasks]


async def _session_event(params: dict) -> dict:
    """A live turn event, or a change in whether the session is waiting on a human."""
    event = params.get("event") or {}
    session_id = str(event.get("context_id") or params.get("session_id") or "")
    if "awaiting_input" in event:
        awaiting = bool(event.get("awaiting_input"))
        if state.registry is not None:
            state.registry.mark(session_id, awaiting_input=awaiting)
        state.broadcaster.publish({"type": "sessions_changed"})
        return {"noted": True}
    part = event.get("part")
    if part is not None:
        # A monotonic sequence per session lets a client order frames and notice a gap, which
        # matters when a watcher attaches mid-turn and joins a stream already in progress.
        _SEQUENCE[session_id] = _SEQUENCE.get(session_id, 0) + 1
        state.event_bus.publish(session_id, {"seq": _SEQUENCE[session_id], "part": part})
    return {"published": True}


_METHODS = {
    "task.save": _task_save,
    "task.get": _task_get,
    "task.delete": _task_delete,
    "turn.save_state": _turn_save_state,
    "turn.load_checkpoint": _turn_load_checkpoint,
    "turn.load_session_state": _turn_load_session_state,
    "turn.tasks_for_context": _turn_tasks_for_context,
    "session.event": _session_event,
}


@router.post("/ingest")
async def ingest(request: Request) -> JSONResponse:
    """One entry point for everything a worker persists or publishes."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "Body must be JSON."}}, status_code=400)
    handler = _METHODS.get(str(payload.get("method") or ""))
    if handler is None:
        return JSONResponse({"error": {"message": "Unknown ingest method."}}, status_code=404)
    try:
        return JSONResponse({"result": await handler(payload.get("params") or {})})
    except Exception as error:  # noqa: BLE001 — a bad write must not take the daemon down
        logger.exception("Ingest call failed")
        return JSONResponse({"error": {"message": str(error)}}, status_code=500)
