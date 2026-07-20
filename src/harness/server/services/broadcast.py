"""Broadcast service: helpers split out of the server runtime."""

from harness.server import state


def _notify_filesystem_lease_state() -> None:
    _publish_broadcast({"type": "sessions_changed"})
    _publish_broadcast({"type": "filesystem_leases_changed"})


def _publish_stream_event(context_id: str, part) -> None:
    """Executor hook: serialize one structured part and fan it out to live viewers."""
    state._event_bus.publish(context_id, part.model_dump(by_alias=True, exclude_none=True, mode="json"))


def _set_turn_state(context_id: str, running: bool) -> None:
    """Track active turns per context and broadcast on the empty/active edge so the
    sidebar reflects which conversations are currently running."""
    previous = state._running_contexts.get(context_id, 0)
    updated = previous + 1 if running else max(0, previous - 1)
    if updated:
        state._running_contexts[context_id] = updated
    else:
        state._running_contexts.pop(context_id, None)
    if (previous == 0) != (updated == 0):
        state._broadcaster.publish({"type": "sessions_changed"})
    # When the last turn for a context finishes, tell live viewers to do a final
    # refresh and close — the structured-part fan-out is only meaningful mid-turn.
    if not running and updated == 0:
        state._event_bus.complete(context_id)


def _notify_permission_state(context_id: str, awaiting: bool) -> None:
    """A turn suspended at (or resumed from) an input-required pause — track it durably
    and refresh the sidebar so it can swap the spinner for an attention marker."""
    if awaiting:
        state._awaiting_input_contexts.add(context_id)
    else:
        state._awaiting_input_contexts.discard(context_id)
    state._broadcaster.publish({"type": "sessions_changed"})


def _publish_broadcast(event: dict) -> None:
    """Publish from either the event-loop thread or a worker thread."""
    if state._main_loop is not None and state._main_loop.is_running():
        state._main_loop.call_soon_threadsafe(state._broadcaster.publish, event)
    else:
        state._broadcaster.publish(event)
