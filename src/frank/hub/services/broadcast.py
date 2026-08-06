"""Publishing to the daemon-wide bus, from either the event-loop thread or a worker thread."""

from __future__ import annotations

from frank.hub import state


def _notify_filesystem_lease_state() -> None:
    """A file lease was taken or released: that changes both what the sidebar shows and what the lease panel lists, so both are refreshed."""
    _publish_broadcast({"type": "sessions_changed"})
    _publish_broadcast({"type": "filesystem_leases_changed"})


def _publish_broadcast(event: dict) -> None:
    """Publish from either the event-loop thread or a worker thread."""
    if state.main_loop is not None and state.main_loop.is_running():
        state.main_loop.call_soon_threadsafe(state.broadcaster.publish, event)
    else:
        state.broadcaster.publish(event)
