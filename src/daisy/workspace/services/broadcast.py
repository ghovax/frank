"""Publishing to the daemon-wide bus, from either the event-loop thread or a worker thread.

Per-session turn state used to live here too, back when the executors ran inside this
process and could call in directly. A session is a process now, so it reports its turn and
input state over the ingest channel and :mod:`daisy.daemon.ingest` records it — the daemon
learns what a session is doing the same way it learns everything else about it.
"""

from __future__ import annotations

from daisy.workspace import state


def _notify_filesystem_lease_state() -> None:
    """A file lease was taken or released: that changes both what the sidebar shows and what
    the lease panel lists, so both are refreshed."""
    _publish_broadcast({"type": "sessions_changed"})
    _publish_broadcast({"type": "filesystem_leases_changed"})


def _publish_broadcast(event: dict) -> None:
    """Publish from either the event-loop thread or a worker thread.

    Several of the daemon's writers run in threads — the SQLite writers, the configuration
    digest — and a queue is not safe to push from one, so the hop through the loop is what
    keeps a broadcast raised off-loop from corrupting a subscriber's queue."""
    if state.main_loop is not None and state.main_loop.is_running():
        state.main_loop.call_soon_threadsafe(state.broadcaster.publish, event)
    else:
        state.broadcaster.publish(event)
