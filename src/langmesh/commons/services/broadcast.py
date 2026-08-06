"""Publishing to the daemon-wide bus, from the event loop or a worker thread."""

from __future__ import annotations

from langmesh.commons import state


def _notify_filesystem_lease_state() -> None:
    """A lease was taken or released, which changes both the sidebar and the lease panel."""
    _publish_broadcast({"type": "sessions_changed"})
    _publish_broadcast({"type": "filesystem_leases_changed"})


def _publish_broadcast(event: dict) -> None:
    """Publish from either thread: a queue is not safe to push from a worker, so this hops through the loop."""
    if state.main_loop is not None and state.main_loop.is_running():
        state.main_loop.call_soon_threadsafe(state.broadcaster.publish, event)
    else:
        state.broadcaster.publish(event)
