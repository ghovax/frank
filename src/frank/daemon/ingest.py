"""Where workers' writes land — the sole-writer intake.

Workers never open the database. They send their persistence calls here, and this module
performs them against the one store the daemon owns. That keeps the append-only store's
single-writer property intact and keeps every write ordered, which is what the row-ordered
history depends on.

Live turn events arrive on the same channel and are fanned out to whoever is attached, so an
event reaches a watcher at the moment it is persisted rather than on some later poll.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from a2a.types import Task

from frank.base.background_tasks import spawn_background_task
from frank.daemon import state

logger = logging.getLogger(__name__)

router = APIRouter()

# Per-session frame counter for the live stream.
_SEQUENCE: dict[str, int] = {}


async def _turn_save(params: dict) -> dict:
    task = Task.model_validate(params.get("task") or {})
    await state.turn_store.save(task)
    return {"saved": task.id}


async def _turn_get(params: dict) -> dict:
    task = await state.turn_store.get(str(params.get("turn_id") or ""))
    return task.model_dump(by_alias=True, exclude_none=True, mode="json") if task else None


async def _turn_delete(params: dict) -> dict:
    await state.turn_store.delete(str(params.get("turn_id") or ""))
    return {"deleted": True}


async def _turn_save_state(params: dict) -> dict:
    await state.turn_store.save_turn_state(
        str(params.get("session_id") or ""),
        str(params.get("turn_id") or ""),
        params.get("messages") or [],
        params.get("session_state"),
    )
    return {"saved": True}


async def _turn_load_checkpoint(params: dict) -> Any:
    return await state.turn_store.load_checkpoint(str(params.get("session_id") or ""))


async def _turn_load_session_state(params: dict) -> Any:
    return await state.turn_store.load_session_state(str(params.get("session_id") or ""))


async def _turn_list_for_session(params: dict) -> Any:
    tasks = await state.turn_store.turns_for_session(str(params.get("session_id") or ""))
    return [task.model_dump(by_alias=True, exclude_none=True, mode="json") for task in tasks]


def _set_turn_state(session_id: str, running: bool, retains: bool = False) -> None:
    """Count the turns a session has in flight, and act on the idle/busy edge.

    A count rather than a flag because a session can have more than one turn open at once —
    a compaction or an autonomous wake alongside the user's — and the sidebar must not go
    quiet when the first of them finishes."""
    previous = state._running_contexts.get(session_id, 0)
    updated = previous + 1 if running else max(0, previous - 1)
    if updated:
        state._running_contexts[session_id] = updated
    else:
        state._running_contexts.pop(session_id, None)
    if (previous == 0) != (updated == 0):
        state.broadcaster.publish({"type": "sessions_changed"})
        # The same edge on the session's own stream, so a watcher learns the turn ended
        # without polling. Turn parts alone never say this — they stop arriving, which is
        # indistinguishable from a model thinking — so `frank wait` had nothing to wait for
        # and blocked until the session died instead of until it went idle.
        _SEQUENCE[session_id] = _SEQUENCE.get(session_id, 0) + 1
        state.event_bus.publish(
            session_id, {"seq": _SEQUENCE[session_id], "turn": {"running": bool(updated)}}
        )
        if not updated and not retains:
            _sleep_when_idle(session_id)


def _sleep_when_idle(session_id: str) -> None:
    """Stop an idle session's process, keeping the session itself.

    **Immediately, with no linger window.** A window would be a cache with a timeout nobody can
    set correctly, and it would be caching against a cost that no longer exists: waking is a
    fork, which is about 60 ms. Keeping a 12 MB interpreter alive on the chance that a message
    arrives is paying continuously to avoid paying occasionally.

    `retains` is the one thing that stops this, and it comes from the worker because only the
    worker can know it: a background job is an in-process `asyncio.Task` and sleeping would
    kill it.

    A session parked on a permission prompt is the clearest case for this and takes a
    different path — it never had a running turn to end — so it is handled where the
    suspension is reported rather than here.
    """
    if state.lifecycle is None:
        return
    record = state.registry.get(session_id) if state.registry is not None else None
    if record is None or not record.is_live or not record.pid:
        return
    spawn_background_task(state.lifecycle.sleep(session_id))


async def _session_claim_work_habits(params: dict) -> dict:
    """The once-per-session work-habits acknowledgement, claimed atomically.

    Here rather than in the worker because a worker is per activation now, and "once per
    session" has to outlive one."""
    from frank.workspace.services.sessions import claim_work_habits_acknowledgement

    session_id = str(params.get("session_id") or "")
    claimed = await asyncio.to_thread(claim_work_habits_acknowledgement, session_id)
    return {"claimed": claimed}


async def _session_event(params: dict) -> dict:
    """A live turn event, or a change in whether the session is waiting on a human."""
    event = params.get("event") or {}
    session_id = str(event.get("session_id") or params.get("session_id") or "")
    if "running" in event:
        # Whether a turn is in flight, which the registry cannot infer: a session's process
        # is alive for its whole life, including while it sits idle between messages.
        _set_turn_state(session_id, bool(event.get("running")), bool(event.get("retains")))
        return {"noted": True}
    if "awaiting_input" in event:
        awaiting = bool(event.get("awaiting_input"))
        if state.registry is not None:
            state.registry.mark(session_id, awaiting_input=awaiting)
        # And the set the *workspace* reads. The registry is the control plane's, and the
        # browser's session list is built by the workspace, which deliberately cannot reach
        # across — so the daemon pushes into it here, exactly as `_set_turn_state` does for
        # running turns. Marking only the registry left the two disagreeing: a session parked
        # on a permission prompt reported `awaiting_input: false` to the interface, so the
        # prompt was never raised and the turn sat waiting on a decision nobody was asked for.
        if awaiting:
            state._awaiting_input_contexts.add(session_id)
        else:
            state._awaiting_input_contexts.discard(session_id)
        state.broadcaster.publish({"type": "sessions_changed"})
        if awaiting:
            # The best case for sleeping, and the one that motivated it. `input-required` is
            # already a durable suspension — the whole turn is checkpointed on disk — and the
            # worker was holding an entire interpreter to wait for a person who may take
            # hours. Answering it wakes a fresh worker and rebuilds the turn from that
            # checkpoint, which is a path that already existed for surviving a restart.
            _sleep_when_idle(session_id)
        return {"noted": True}
    part = event.get("part")
    if part is not None:
        # A monotonic sequence per session lets a client order frames and notice a gap, which
        # matters when a watcher attaches mid-turn and joins a stream already in progress.
        _SEQUENCE[session_id] = _SEQUENCE.get(session_id, 0) + 1
        state.event_bus.publish(session_id, {"seq": _SEQUENCE[session_id], "part": part})
    return {"published": True}


async def _session_title(params: dict) -> dict:
    """A title a session generated for itself.

    Produced in the worker because producing it means calling a model, which is the runtime's
    job and not the control plane's. Written here because the daemon is the sole writer."""
    from frank.workspace.services.sessions import _set_session_title

    session_id = str(params.get("session_id") or "")
    title = str(params.get("title") or "").strip()
    if not session_id or not title:
        return {"saved": False}
    changed = await asyncio.to_thread(_set_session_title, session_id, title)
    if changed:
        # Both views of a session carry the title: the durable row the GUI lists from, and
        # the registry the CLI reads, which would otherwise keep showing a directory for a
        # session that has since named itself.
        if state.registry is not None:
            state.registry.mark(session_id, title=title)
        state.broadcaster.publish({"type": "sessions_changed"})
    return {"saved": changed}


_METHODS = {
    "turn.save": _turn_save,
    "turn.get": _turn_get,
    "turn.delete": _turn_delete,
    "turn.save_state": _turn_save_state,
    "session.claim_work_habits": _session_claim_work_habits,
    "turn.load_checkpoint": _turn_load_checkpoint,
    "turn.load_session_state": _turn_load_session_state,
    "turn.list_for_session": _turn_list_for_session,
    "session.event": _session_event,
    "session.title": _session_title,
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
