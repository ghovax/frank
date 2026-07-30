"""Starting sessions, watching them, and reaping them together.

Three things live here because they are the same concern seen from different ends: asking the
prototype to fork a session into being, noticing when a session's process dies without telling
anyone, and taking a session down along with everything it created.

Reaping is depth-first over the tree and always kills children before the parent, so a child
is never briefly orphaned onto a dead parent. Each session leads its own process session, so a
session that started a dev server takes that dev server with it.

Noticing a death is the part that changed shape. The daemon used to `await process.wait()` on
a worker it had spawned itself; it does not spawn them any more, and a process cannot be
waited on by anything but its parent. The parent is the prototype, so deaths arrive here as
reports rather than as returns — one handler for every session instead of one supervisor task
each, which is the same information through a different door.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from datetime import datetime, timezone
from typing import Callable, Optional

from frank.base.paths import session_socket_path
from frank.daemon.peer_identity import session_process_groups
from frank.daemon.prototype import PrototypeClient, PrototypeUnavailable, SessionExit
from frank.daemon.registry import EXITED, FAILED, SessionRecord, SessionRegistry
from frank.daemon.state import relay_to_session
from frank.protocol.metadata import Metadata
from frank.base.tuning import Tunable, active_tuning

logger = logging.getLogger(__name__)


def _daemon_token() -> str:
    from frank.daemon import state

    return state.daemon_token


def _resolve_locations(session_id: str):
    """The workspace's locations, in the shape the runtime builds executors from.

    Best effort: a session with no project has none, and a lookup that fails must not stop the
    session from starting — the runtime falls back to a single local location at its working
    directory, which is exactly right for a session that has no project."""
    from frank.hub.services.locations import _resolve_session_locations

    try:
        return _resolve_session_locations(session_id)
    except Exception:  # noqa: BLE001 — a session without locations still runs
        logger.warning("Could not resolve locations for %s", session_id, exc_info=True)
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _close_subscribers(session_id: str) -> None:
    """Tell every subscriber of this session's stream that it has ended.

    Without it an `frank attach` on a session that is killed — or that dies on its own — sits
    on a stream that will never produce another frame, because the only thing that ever closed
    it was the session speaking. A subscriber must learn about the deaths too."""
    from frank.daemon import state

    with contextlib.suppress(Exception):
        state.event_bus.complete(session_id)


class SessionLifecycle:
    """Owns the transition of a fork into a live session, and back out again."""

    def __init__(
        self,
        registry: SessionRegistry,
        prototype: PrototypeClient,
        *,
        on_change: Optional[Callable[[], None]] = None,
    ) -> None:
        self._registry = registry
        self._prototype = prototype
        self._on_change = on_change
        # session id -> the process id the prototype forked for it.
        self._processes: dict[str, int] = {}
        # session id -> a future settled when that session's exit is reported, so a reap can
        # wait for the process to actually be gone rather than for a timeout to elapse.
        self._departures: dict[str, asyncio.Future] = {}
        # Sessions whose process this file is deliberately stopping in order to *sleep* them.
        # Without it a sleep is indistinguishable from a crash: the exit report arrives the
        # same way for both, finds a record that is still live, and ends the session — which
        # is the opposite of what sleeping means.
        self._sleeping: set[str] = set()

    def _changed(self) -> None:
        if self._on_change is not None:
            with contextlib.suppress(Exception):
                self._on_change()

    async def start(self, record: SessionRecord) -> bool:
        """Turn a minted record into a running session.

        Asks the prototype to fork, and answers only once the child reports that its socket is
        accepting connections. Waiting for that acknowledgement is what lets a client `send`
        immediately after `create` without racing the bind."""
        assignment = {
            "session_id": record.id,
            "agent": record.agent,
            "working_directory": record.working_directory,
            # Where the session's tools run, which is not always where its project lives: a
            # worktree workspace puts them somewhere else entirely, and the daemon resolved
            # that when the session was created.
            "runtime_working_directory": record.runtime_working_directory or record.working_directory,
            "permission_mode": record.permission_mode,
            # Already resolved and clamped by `session.create`. The session applies it to
            # every child it spawns and never widens it, so it travels with the session rather
            # than being read again from a configuration file that may have changed since.
            "sandbox": record.sandbox,
            "workspace_id": record.workspace_id,
            # Resolved once, here, rather than by the session asking back. A project's
            # locations are fixed for a session's life in the same way its sandbox and its
            # permission mode are, and the injection that was supposed to let a worker resolve
            # them was never wired to anything — so multi-location projects were dead at the
            # session level and every runtime synthesised a single local location.
            "locations": _resolve_locations(record.id),
            "parent": record.parent,
            "token": record.token,
            # The daemon's own token, so the session can write to the ingest endpoint. Its
            # session token authorises calls *to* it, which is the other direction entirely.
            "daemon_token": _daemon_token(),
            "socket": str(session_socket_path(record.id)),
        }
        try:
            pid = await self._prototype.fork_session(assignment)
        except PrototypeUnavailable as error:
            logger.error("Could not start session %s: %s", record.id, error)
            self._registry.end(record.id, outcome=FAILED, reason=str(error), updated_at=_now())
            self._changed()
            return False

        self._processes[record.id] = pid
        self._registry.mark(record.id, pid=pid, updated_at=_now())
        self._changed()
        return True

    async def on_session_exit(self, report: SessionExit) -> None:
        """A session's process has ended, however it ended.

        This is what the per-session supervisor task used to be. A session that exits on its
        own — a crash, an unhandled error, an OOM kill — would otherwise leave a record
        claiming to be running and a socket nothing is listening on, and the daemon cannot
        watch for that itself: it did not fork the process, so it cannot wait on it. The
        prototype did, and reports here.

        Reached for every death, including the ones this file caused. A reaped session was
        already marked terminal before it was signalled, so the `is_live` check is what tells
        the two apart."""
        session_id = report.session_id
        if not session_id:
            return
        self._processes.pop(session_id, None)
        departure = self._departures.pop(session_id, None)
        if departure is not None and not departure.done():
            departure.set_result(None)
        if session_id in self._sleeping:
            # A death this file caused on purpose. The session keeps its record and its
            # subscribers; only the process is gone.
            return
        record = self._registry.get(session_id)
        if record is not None and record.is_live:
            if not report.clean:
                # Said out loud, because until now it was not. The reason went onto the record
                # and into the database, where nothing reads it until a client asks about that
                # session — so a worker dying on every single fork looked, from the log, like
                # nothing happening at all. The client is told "could not start the turn"; this
                # is the line that says why.
                logger.error(
                    "Session %s died: %s", session_id, report.describe(),
                )
            self._registry.end(
                session_id,
                outcome=EXITED if report.clean else FAILED,
                reason="" if report.clean else report.describe(),
                updated_at=_now(),
            )
            # A dead parent takes its children with it, exactly as an explicit kill would.
            await self.reap(session_id, reason="parent session ended", skip_self=True)
            # Read back rather than reused: `mark` is what put the terminal status and reason
            # on the record, and the notice reports those.
            ended = self._registry.get(session_id)
            if ended is not None:
                await self._tell_parent(ended)
        _close_subscribers(session_id)
        self._unlink_socket(session_id)
        self._changed()

    async def _tell_parent(self, record) -> None:
        """Tell a session that one of its children is over.

        A peer reports its own result by messaging the session that created it, which is the
        whole return path — so a peer that crashes, is killed, or exits mid-turn leaves its
        caller waiting for a message that will never be sent. Only the daemon can close that
        gap, because a session cannot report its own death.

        Deliberately the only message the harness writes on a session's behalf. Everything a
        peer *can* say, it says itself; this covers exactly the case where it cannot. Best
        effort, and quiet on failure: a parent that has itself gone is the common reason this
        does not land, and it is not an error."""
        parent = self._registry.get(record.parent) if record.parent else None
        if parent is None or not parent.is_live:
            return
        outcome = record.exit_reason or ("finished" if record.outcome == EXITED else record.outcome)
        text = (
            f"Session {record.id} ({record.agent}), which you created, has ended without "
            f"reporting back: {outcome}."
        )
        try:
            await relay_to_session(parent, "message/send", {
                "id": parent.id,
                "parts": [{"kind": "text", "text": text}],
                "metadata": {Metadata.PEER_SENDER: record.id},
            })
        except Exception:  # noqa: BLE001 — a notice that cannot be delivered is not a failure
            logger.debug("Could not tell %s that %s ended", parent.id, record.id, exc_info=True)

    async def reap(self, session_id: str, *, reason: str = "", skip_self: bool = False) -> int:
        """Take a session and everything under it down.

        Children first, so a child never observes a dead parent, and the whole process group
        is signalled so a session's shell subtree dies with it."""
        record = self._registry.get(session_id)
        if record is None:
            return 0
        descendants = [record for record in self._registry.descendants_of(session_id) if record.is_live]
        for descendant in descendants:
            self._registry.end(
                descendant.id, outcome=EXITED, updated_at=_now(),
                reason=reason or "parent session was reaped",
            )
        # Stopped together rather than one at a time: each carries its own grace period, and a
        # wide tree would otherwise take that period multiplied by its size to come down.
        await asyncio.gather(*(self._stop(record.id) for record in descendants), return_exceptions=True)
        reaped = len(descendants)
        if not skip_self and record.is_live:
            self._registry.end(session_id, outcome=EXITED, reason=reason, updated_at=_now())
            await self._stop(session_id)
            reaped += 1
        self._changed()
        # After the stops, so the notice describes a session that is actually over. Only for
        # the session that was asked about: a descendant's parent is inside this same subtree
        # and is going down with it, so telling it would be writing to a corpse.
        if not skip_self:
            await self._tell_parent(self._registry.get(session_id) or record)
        return reaped

    async def sleep(self, session_id: str) -> bool:
        """Take a live session's process away, leaving the session itself intact.

        The difference from :meth:`reap` is the whole of Part III: reaping ends the session,
        this ends only its process. The record stays `live`, its activity becomes `asleep`, and
        the next command routed through `wake_then_relay` forks it a new worker from the
        prototype — a fresh copy, never a reused one, so the isolation invariant is untouched.

        Everything the woken worker needs is already on the record, because all of it was fixed
        at `create` and none of it is re-derived; the conversation itself comes back from the
        turn store's checkpoint, which is the same path a restart already used.
        """
        record = self._registry.get(session_id)
        if record is None or not record.is_live:
            return False
        pid = self._processes.pop(session_id, None)
        if pid is None:
            return False
        logger.info("Sleeping session %s (pid %d)", session_id, pid)
        self._sleeping.add(session_id)
        try:
            await self._terminate(session_id, pid)
        finally:
            self._sleeping.discard(session_id)
        self._departures.pop(session_id, None)
        self._registry.sleep(session_id, updated_at=_now())
        # Deliberately not closing subscribers and not unlinking the socket the way `_stop`
        # does. An `attach` on a sleeping session stays open — the session has not ended, and a
        # watcher should see the next turn when it wakes rather than having to reconnect.
        with contextlib.suppress(OSError):
            session_socket_path(session_id).unlink(missing_ok=True)
        self._changed()
        return True

    async def _stop(self, session_id: str) -> None:
        pid = self._processes.pop(session_id, None)
        if pid is not None:
            await self._terminate(session_id, pid)
        self._departures.pop(session_id, None)
        _close_subscribers(session_id)
        self._unlink_socket(session_id)

    async def _terminate(self, session_id: str, pid: int) -> None:
        """SIGTERM everything in the session's process session, then SIGKILL what is left.

        The unit here has to be the process *session*, not the process group. A session leads
        its own — `setsid` in the forked child — and everything it runs inherits that session
        id, but the bash tool puts each command in a group of its own, deliberately, so one job
        can be cancelled with its whole subtree. Signalling only the session's own group
        therefore missed every command it ran: end a session that started a dev server and the
        port stayed held.

        So the groups are enumerated by session membership and each is signalled. The
        enumeration happens once, before anything is signalled, because a session's children
        are reparented the moment it dies — they keep the session id, so a later sweep would
        still find them, but there is no reason to make correctness depend on that.

        Waiting for the death is a report from the prototype rather than a `waitpid`, because
        the daemon is not the parent. Polling the pid would be the alternative and would be
        wrong in a specific way: a reaped-but-not-yet-collected process still answers
        `kill(pid, 0)`, so a poll would either race or need a timeout where this needs none."""
        departure: asyncio.Future = asyncio.get_running_loop().create_future()
        self._departures[session_id] = departure

        groups = await asyncio.to_thread(session_process_groups, pid)
        if not groups:
            # No listing available (an unexpected platform, `ps` missing). Falling back to the
            # session's own group keeps its death reliable, and says why it is less than it
            # should be rather than silently reaping too little.
            logger.warning(
                "Could not enumerate the process session of %d; signalling its group only", pid
            )
            with contextlib.suppress(OSError):
                groups = [os.getpgid(pid)]

        def signal_groups(number: int) -> None:
            for group in groups:
                try:
                    os.killpg(group, number)
                except (ProcessLookupError, PermissionError):
                    continue

        signal_groups(signal.SIGTERM)
        if not groups:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
        grace = active_tuning().duration(Tunable.sigterm_grace_seconds)
        try:
            await asyncio.wait_for(asyncio.shield(departure), timeout=grace)
            return
        except asyncio.TimeoutError:
            pass
        signal_groups(signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(departure), timeout=grace)

    def _unlink_socket(self, session_id: str) -> None:
        """Remove a dead session's socket file so nothing connects to a corpse."""
        with contextlib.suppress(OSError):
            session_socket_path(session_id).unlink(missing_ok=True)

    async def sleep_all(self) -> int:
        """Stop every session's process, keeping every session.

        What a daemon shutdown actually needs to do now that the registry is durable. A worker
        cannot outlive its supervisor — it persists through the daemon, so an orphan would
        silently lose work — but the *session* has no reason to end with it."""
        running = self._registry.running()
        await asyncio.gather(
            *(self.sleep(record.id) for record in running), return_exceptions=True,
        )
        return len(running)

    async def aclose(self) -> None:
        """Put every running session to sleep. Called when the daemon itself is going down.

        Deliberately sleeping rather than reaping. A session used to end here because its
        record lived in this process and would not survive; the record is durable now, so
        ending it would be discarding something the next daemon can pick straight back up."""
        await self.sleep_all()
