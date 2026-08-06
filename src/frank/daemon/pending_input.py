"""Settling a session that is parked on a human decision.

Supervision rather than workspace: both of these relay to a *live* session, waking it first if
it is asleep — which a session parked on a permission prompt almost always is, because that is
the case sleeping was built for.

They live here because the browser surface must not reach the control plane. Deleting a session
from the sidebar should settle whatever it was waiting on and then stop it, and the way that
happens is `workspace.state.on_session_deleted`, which the daemon's composition root points at
:func:`settle_and_reap`. A workspace served without a daemon simply has no hook to call.
"""

from __future__ import annotations

import logging

from frank.daemon import state

logger = logging.getLogger(__name__)


async def _abort_pending_input(session_id: str) -> bool:
    """Deny every gate a session is parked on, so its turn resumes and records the denials
    rather than leaving a checkpoint no later turn could build on.

    Wakes the session to do it. A session parked on a permission prompt is exactly the case
    that sleeps — its whole state is on disk and it was holding an interpreter to wait — so the
    gate it is parked on almost always belongs to a session with no process."""
    record = state.registry.get(session_id) if state.registry is not None else None
    if record is None:
        return False
    try:
        result = await state.wake_then_relay(record, "input/abort", {})
    except Exception:  # noqa: BLE001
        return False
    return bool(result.get("aborted"))


async def settle_and_reap(session_id: str) -> None:
    """What deleting a session's record means for the session itself.

    Deny whatever it is parked on first, so its turn resumes and records the denials rather
    than leaving a checkpoint no later turn could build on, then end it. The record's own rows
    are removed by the caller either way — a session whose process cannot be reached is still
    a session the user asked to delete.
    """
    from frank.hub import state as hub_state

    await _abort_pending_input(session_id)
    state._awaiting_input_contexts.discard(session_id)
    if state.lifecycle is not None:
        await state.lifecycle.reap(session_id, reason="session deleted")
    hub_state.broadcaster.publish({"type": "sessions_changed"})


__all__ = ["settle_and_reap"]
