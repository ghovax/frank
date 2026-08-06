"""Starting sessions, watching them, and reaping them together, as one concern seen from three ends."""

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
    """The workspace's locations in the shape the runtime builds executors from, best effort."""
    from frank.hub.services.locations import _resolve_session_locations

    try:
        return _resolve_session_locations(session_id)
    except Exception:  # noqa: BLE001 — a session without locations still runs
        logger.warning("could not resolve locations for %s", session_id, exc_info=True)
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _close_subscribers(session_id: str) -> None:
    """Tell every subscriber of this session's stream that it has ended, so an attach does not hang forever."""
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
        # Session id to a future settled when its exit is reported, so a reap waits for the process rather than a timeout.
        self._departures: dict[str, asyncio.Future] = {}
        # Sessions this file is deliberately stopping in order to sleep them, which an exit report cannot otherwise tell from a crash.
        self._sleeping: set[str] = set()

    def _changed(self) -> None:
        if self._on_change is not None:
            with contextlib.suppress(Exception):
                self._on_change()

    async def start(self, record: SessionRecord) -> bool:
        """Turn a minted record into a running session, answering once the child reports it is accepting connections."""
        assignment = {
            "session_id": record.id,
            "agent": record.agent,
            "working_directory": record.working_directory,
            # Where the session's tools run, which a worktree workspace puts somewhere other than where its project lives.
            "runtime_working_directory": record.runtime_working_directory or record.working_directory,
            "permission_mode": record.permission_mode,
            # Already resolved and clamped at creation, so it travels with the session rather than being read again.
            "sandbox": record.sandbox,
            "workspace_id": record.workspace_id,
            # Resolved once here, since a project's locations are fixed for a session's life as its sandbox and mode are.
            "locations": _resolve_locations(record.id),
            "parent": record.parent,
            "token": record.token,
            # The daemon's own token, so the session can write to the ingest endpoint.
            "daemon_token": _daemon_token(),
            "socket": str(session_socket_path(record.id)),
        }
        try:
            pid = await self._prototype.fork_session(assignment)
        except PrototypeUnavailable as error:
            logger.error("could not start session %s", record.id, exc_info=True)
            self._registry.end(record.id, outcome=FAILED, reason=str(error), updated_at=_now())
            self._changed()
            return False

        self._processes[record.id] = pid
        self._registry.mark(record.id, pid=pid, updated_at=_now())
        self._changed()
        return True

    async def on_prototype_lost(self) -> None:
        """The prototype died, so every session it forked died with it, each having stopped when its lifeline broke."""
        losses = list(self._processes.items())
        if not losses:
            return
        logger.error(
            "the prototype died; %d session(s) went with it and are being ended.", len(losses)
        )
        for session_id, pid in losses:
            await self.on_session_exit(
                SessionExit(session_id=session_id, pid=pid, code=-1, signal=signal.SIGKILL)
            )

    async def on_session_exit(self, report: SessionExit) -> None:
        """A session's process has ended, however it ended, which is what the per-session supervisor used to notice."""
        session_id = report.session_id
        if not session_id:
            return
        self._processes.pop(session_id, None)
        departure = self._departures.pop(session_id, None)
        if departure is not None and not departure.done():
            departure.set_result(None)
        if session_id in self._sleeping:
            # A death this file caused on purpose: the session keeps its record and subscribers, and only the process is gone.
            return
        record = self._registry.get(session_id)
        if record is not None and record.is_live:
            if not report.clean:
                # Said out loud, because a reason on the record is read by nothing until somebody asks about that session.
                logger.error(
                    "session %s died: %s", session_id, report.describe(),
                )
            self._registry.end(
                session_id,
                outcome=EXITED if report.clean else FAILED,
                reason="" if report.clean else report.describe(),
                updated_at=_now(),
            )
            # A dead parent takes its children with it, exactly as an explicit kill would.
            await self.reap(session_id, reason="parent session ended", skip_self=True)
            # Read back rather than reused, since `mark` is what put the terminal status and reason on the record.
            ended = self._registry.get(session_id)
            if ended is not None:
                await self._tell_parent(ended)
        _close_subscribers(session_id)
        self._unlink_socket(session_id)
        self._changed()

    async def _tell_parent(self, record) -> None:
        """Tell a session that one of its children is over, since a peer's own report is the whole return path."""
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
            logger.debug("could not tell %s that %s ended", parent.id, record.id, exc_info=True)

    async def reap(self, session_id: str, *, reason: str = "", skip_self: bool = False) -> int:
        """Take a session and everything under it down, children first and by process group."""
        record = self._registry.get(session_id)
        if record is None:
            return 0
        descendants = [record for record in self._registry.descendants_of(session_id) if record.is_live]
        # A goal describes work in progress, so it ends with the session that was pursuing it.
        from frank.base import toolbox
        from frank.daemon import state as daemon_state

        for ending in ([] if skip_self else [record]) + descendants:
            daemon_state._session_goals.pop(ending.id, None)
            toolbox.discard(ending.id)
        for descendant in descendants:
            self._registry.end(
                descendant.id, outcome=EXITED, updated_at=_now(),
                reason=reason or "parent session was reaped",
            )
        # Stopped together rather than one at a time, since each carries its own grace period.
        await asyncio.gather(*(self._stop(record.id) for record in descendants), return_exceptions=True)
        reaped = len(descendants)
        if not skip_self and record.is_live:
            self._registry.end(session_id, outcome=EXITED, reason=reason, updated_at=_now())
            await self._stop(session_id)
            reaped += 1
        self._changed()
        # After the stops, so the notice describes a session that is actually over, and only for the one asked about.
        if not skip_self:
            await self._tell_parent(self._registry.get(session_id) or record)
        return reaped

    async def sleep(self, session_id: str) -> bool:
        """Take a live session's process away, leaving the session itself intact."""
        record = self._registry.get(session_id)
        if record is None or not record.is_live:
            return False
        pid = self._processes.pop(session_id, None)
        if pid is None:
            return False
        logger.info("sleeping session %s (pid %d)", session_id, pid)
        self._sleeping.add(session_id)
        try:
            await self._terminate(session_id, pid)
        finally:
            self._sleeping.discard(session_id)
        self._departures.pop(session_id, None)
        self._registry.sleep(session_id, updated_at=_now())
        # Deliberately not closing subscribers, since a watcher should see the next turn when the session wakes.
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
        """Signal everything in the session's process session, which is the unit a session's whole shell subtree belongs to."""
        departure: asyncio.Future = asyncio.get_running_loop().create_future()
        self._departures[session_id] = departure

        groups = await asyncio.to_thread(session_process_groups, pid)
        if not groups:
            # No listing available, so fall back to the session's own group and say why it is less than it should be.
            logger.warning(
                "could not enumerate the process session of %d; signalling its group only", pid
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
        """Stop every session's process while keeping every session, which is what a durable registry makes shutdown mean."""
        running = self._registry.running()
        await asyncio.gather(
            *(self.sleep(record.id) for record in running), return_exceptions=True,
        )
        return len(running)

    async def aclose(self) -> None:
        """Put every running session to sleep when the daemon goes down, sleeping rather than reaping."""
        await self.sleep_all()
