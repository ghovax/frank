"""Where workers' writes land: they send their persistence calls here, and the daemon performs them."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from a2a.types import Task

from langmesh.base.tuning import Tunable, active_tuning
from langmesh.daemon import state

logger = logging.getLogger(__name__)

router = APIRouter()

# Per-session frame counter for the live stream.
_SEQUENCE: dict[str, int] = {}


async def _turn_save(params: dict) -> dict:
    # The task arrives as itself, since the session that built it runs here; only a stranger would need JSON.
    task = params.get("task")
    if not isinstance(task, Task):
        task = Task.model_validate(task or {})
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
        str(params.get("inherited_snapshot_id") or ""),
    )
    return {"saved": True}


async def _turn_save_session_state(params: dict) -> dict:
    await state.turn_store.save_session_state(
        str(params.get("session_id") or ""), params.get("session_state") or {},
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
    """Count the turns a session has in flight, and act on the idle and busy edge."""
    if running:
        # Used again, so any pending idle timer is stale.
        cancel_idle_sleep(session_id)
    previous = state._running_contexts.get(session_id, 0)
    updated = previous + 1 if running else max(0, previous - 1)
    if updated:
        state._running_contexts[session_id] = updated
    else:
        state._running_contexts.pop(session_id, None)
    if (previous == 0) != (updated == 0):
        state.broadcaster.publish({"type": "sessions_changed"})
        # The same edge on the session's own stream, so a watcher learns the turn ended without polling.
        _SEQUENCE[session_id] = _SEQUENCE.get(session_id, 0) + 1
        state.event_bus.publish(
            session_id, {"seq": _SEQUENCE[session_id], "turn": {"running": bool(updated)}}
        )
        if not updated and not retains:
            _sleep_when_idle(session_id)


# One pending idle timer per session, cancelled by anything that touches the session again.
_IDLE_TIMERS: dict[str, asyncio.Task] = {}


def cancel_idle_sleep(session_id: str) -> None:
    """The session was used again, so it is no longer idle."""
    timer = _IDLE_TIMERS.pop(session_id, None)
    if timer is not None and not timer.done():
        timer.cancel()


async def _sleep_after_idle(session_id: str, delay: float) -> None:
    """Wait out the idle window, then stop the process if nothing intervened."""
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    _IDLE_TIMERS.pop(session_id, None)
    if state.lifecycle is None or state.registry is None:
        return
    record = state.registry.get(session_id)
    if record is None or not record.is_live or not record.hosted or record.busy:
        return
    logger.info("session %s idle for %.0fs; sleeping it", session_id, delay)
    await state.lifecycle.sleep(session_id)


def _sleep_when_idle(session_id: str) -> None:
    """Stop an idle session's process after the idle window, keeping the session itself."""
    if state.lifecycle is None:
        return
    record = state.registry.get(session_id) if state.registry is not None else None
    if record is None or not record.is_live or not record.hosted:
        return
    cancel_idle_sleep(session_id)
    delay = active_tuning().duration(Tunable.session_idle_sleep)
    _IDLE_TIMERS[session_id] = asyncio.create_task(_sleep_after_idle(session_id, delay))


async def _session_claim_work_habits(params: dict) -> dict:
    """The once-per-session work-habits acknowledgement, claimed atomically because a worker is per activation."""
    from langmesh.commons.services.sessions import claim_work_habits_acknowledgement

    session_id = str(params.get("session_id") or "")
    claimed = await asyncio.to_thread(claim_work_habits_acknowledgement, session_id)
    return {"claimed": claimed}


async def _session_event(params: dict) -> dict:
    """A live turn event, or a change in whether the session is waiting on a human."""
    event = params.get("event") or {}
    session_id = str(event.get("session_id") or params.get("session_id") or "")
    if "running" in event:
        # Whether a turn is in flight, which the registry cannot infer from a process that is alive either way.
        _set_turn_state(session_id, bool(event.get("running")), bool(event.get("retains")))
        return {"noted": True}
    if "awaiting_input" in event:
        awaiting = bool(event.get("awaiting_input"))
        if state.registry is not None:
            state.registry.mark(session_id, awaiting_input=awaiting)
        # And the set the workspace reads, which the daemon pushes into because the two layers cannot reach across.
        if awaiting:
            state._awaiting_input_contexts.add(session_id)
        else:
            state._awaiting_input_contexts.discard(session_id)
        state.broadcaster.publish({"type": "sessions_changed"})
        if awaiting:
            # The best case for sleeping: an input-required pause is already checkpointed on disk.
            _sleep_when_idle(session_id)
        return {"noted": True}
    if "goal" in event:
        # The session's goal changed, held beside the registry because a stored goal would outlive its worker.
        goal = event.get("goal")
        if isinstance(goal, dict):
            state._session_goals[session_id] = goal
        else:
            state._session_goals.pop(session_id, None)
        state.broadcaster.publish({"type": "sessions_changed"})
        return {"noted": True}
    part = event.get("part")
    if part is not None:
        # A monotonic sequence per session lets a client order frames and notice a gap.
        _SEQUENCE[session_id] = _SEQUENCE.get(session_id, 0) + 1
        state.event_bus.publish(session_id, {"seq": _SEQUENCE[session_id], "part": part})
    return {"published": True}


async def _session_usage(params: dict) -> dict:
    """A subscription's rate-limit snapshot as a worker read it, captured there and served from here."""
    from langmesh.base import subscription

    usage = params.get("usage")
    subscription.set_usage_snapshot(usage if isinstance(usage, dict) else None)
    return {"noted": True}


async def _session_title(params: dict) -> dict:
    """A title a session generated for itself, produced in the worker because it means calling a model."""
    from langmesh.commons.services.sessions import _set_session_title

    session_id = str(params.get("session_id") or "")
    title = str(params.get("title") or "").strip()
    if not session_id or not title:
        return {"saved": False}
    # A name somebody chose is not a name to improve on.
    existing = state.registry.get(session_id) if state.registry is not None else None
    if existing is not None and (existing.title or "").strip():
        return {"saved": False}
    changed = await asyncio.to_thread(_set_session_title, session_id, title)
    if changed:
        # Both views carry the title: the durable row the interface lists from, and the registry the command line reads.
        if state.registry is not None:
            state.registry.mark(session_id, title=title)
        state.broadcaster.publish({"type": "sessions_changed"})
    return {"saved": changed}


async def _session_append_ledger(params: dict) -> dict:
    """Append entries to a session's ledger. Append-only, so this neither revises nor removes."""
    if state.turn_store is None:
        return {"appended": 0}
    entries = params.get("entries") or []
    appended = await state.turn_store.append_ledger(
        str(params.get("session_id") or ""), str(params.get("ledger") or "observations"), list(entries),
    )
    return {"appended": appended}


async def _session_ledger(params: dict) -> list:
    """Read a session's ledger, live entries only unless the whole chain is asked for."""
    if state.turn_store is None:
        return []
    return await state.turn_store.ledger_entries(
        str(params.get("session_id") or ""),
        str(params.get("ledger") or "observations"),
        live_only=bool(params.get("live_only", True)),
    )


_METHODS = {
    "turn.save": _turn_save,
    "turn.get": _turn_get,
    "turn.delete": _turn_delete,
    "turn.save_state": _turn_save_state,
    "turn.save_session_state": _turn_save_session_state,
    "session.claim_work_habits": _session_claim_work_habits,
    "turn.load_checkpoint": _turn_load_checkpoint,
    "turn.load_session_state": _turn_load_session_state,
    "turn.list_for_session": _turn_list_for_session,
    "session.event": _session_event,
    "session.title": _session_title,
    "session.usage": _session_usage,
    "session.append_ledger": _session_append_ledger,
    "session.ledger": _session_ledger,
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
        logger.exception("ingest call failed")
        return JSONResponse({"error": {"message": str(error)}}, status_code=500)
