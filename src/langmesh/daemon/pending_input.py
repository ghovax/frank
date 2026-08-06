"""Settling a session parked on a human decision, waking it first because a parked session is asleep."""

from __future__ import annotations

import logging

from langmesh.daemon import state

logger = logging.getLogger(__name__)


async def _abort_pending_input(session_id: str) -> bool:
    """Deny every gate a session is parked on, so its turn resumes and records the denials."""
    record = state.registry.get(session_id) if state.registry is not None else None
    if record is None:
        return False
    try:
        result = await state.wake_then_relay(record, "input/abort", {})
    except Exception:  # noqa: BLE001
        return False
    return bool(result.get("aborted"))


async def settle_and_reap(session_id: str) -> None:
    """What deleting a record means for the session: settle what it is parked on, then end it."""
    from langmesh.commons import state as commons_state

    await _abort_pending_input(session_id)
    state._awaiting_input_contexts.discard(session_id)
    if state.lifecycle is not None:
        await state.lifecycle.reap(session_id, reason="session deleted")
    commons_state.broadcaster.publish({"type": "sessions_changed"})


__all__ = ["settle_and_reap"]
