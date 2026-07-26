"""Starting sessions, watching them, and reaping them together.

Three things live here because they are the same concern seen from different ends: handing a
warm worker its assignment so it becomes a session, noticing when a session's process dies
without telling anyone, and taking a session down along with everything it created.

Reaping is depth-first over the tree and always kills children before the parent, so a child
is never briefly orphaned onto a dead parent. Each worker leads its own process group, so a
session that started a dev server takes that dev server with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from datetime import datetime, timezone
from typing import Callable, Optional

from xeac.base.paths import session_socket_path
from xeac.daemon.peer_identity import session_process_groups
from xeac.daemon.pool import WarmWorker, WorkerPool
from xeac.daemon.registry import EXITED, FAILED, RUNNING, SessionRecord, SessionRegistry
from xeac.daemon.state import relay_to_session
from xeac.protocol.metadata import Metadata
from xeac.base.tuning import Limit, active_tuning

logger = logging.getLogger(__name__)


def _daemon_token() -> str:
    from xeac.daemon import state

    return state.daemon_token


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _close_subscribers(session_id: str) -> None:
    """Tell every subscriber of this session's stream that it has ended.

    Without it an `xeac attach` on a session that is killed — or that dies on its own — sits
    on a stream that will never produce another frame, because the only thing that ever closed
    it was the session speaking. A subscriber must learn about the deaths too."""
    from xeac.daemon import state

    with contextlib.suppress(Exception):
        state.event_bus.complete(session_id)


class SessionLifecycle:
    """Owns the transition of a warm worker into a live session, and back out again."""

    def __init__(
        self,
        registry: SessionRegistry,
        pool: WorkerPool,
        *,
        on_change: Optional[Callable[[], None]] = None,
    ) -> None:
        self._registry = registry
        self._pool = pool
        self._on_change = on_change
        self._processes: dict[str, WarmWorker] = {}
        self._supervisors: dict[str, asyncio.Task] = {}

    def _changed(self) -> None:
        if self._on_change is not None:
            with contextlib.suppress(Exception):
                self._on_change()

    async def start(self, record: SessionRecord) -> bool:
        """Turn a minted record into a running session.

        Claims a warm worker, writes it its assignment, and waits for it to report that its
        socket is accepting connections. Waiting for that acknowledgement is what lets a
        client `send` immediately after `create` without racing the worker's bind."""
        worker = await self._pool.claim()
        if worker is None:
            self._registry.mark(record.id, status=FAILED, updated_at=_now(), exit_reason="no worker available")
            self._changed()
            return False

        assignment = {
            "session_id": record.id,
            "agent": record.agent,
            "working_directory": record.working_directory,
            # Where the session's tools run, which is not always where its project lives: a
            # worktree workspace puts them somewhere else entirely, and the daemon resolved
            # that when the session was created.
            "runtime_working_directory": record.runtime_working_directory or record.working_directory,
            "permission_mode": record.permission_mode,
            # Already resolved and clamped by `session.create`. The worker applies it to every
            # child it spawns and never widens it, so it travels with the session rather than
            # being read again from a configuration file that may have changed since.
            "sandbox": record.sandbox,
            "project_id": record.project_id,
            "parent": record.parent,
            "token": record.token,
            # The daemon's own token, so the worker can write to the ingest endpoint. Its
            # session token authorises calls *to* it, which is the other direction entirely.
            "daemon_token": _daemon_token(),
            "socket": str(session_socket_path(record.id)),
        }
        try:
            assert worker.process.stdin is not None
            worker.process.stdin.write((json.dumps(assignment) + "\n").encode())
            await worker.process.stdin.drain()
        except (OSError, AssertionError) as error:
            logger.error("Could not assign session %s to worker %d: %s", record.id, worker.pid, error)
            self._registry.mark(record.id, status=FAILED, updated_at=_now(), exit_reason="worker refused assignment")
            self._changed()
            return False

        ready = await self._await_ready(worker)
        if not ready:
            self._registry.mark(record.id, status=FAILED, updated_at=_now(), exit_reason="worker did not become ready")
            await self._terminate(worker)
            self._pool.release()
            self._changed()
            return False

        self._processes[record.id] = worker
        self._registry.mark(record.id, status=RUNNING, updated_at=_now(), pid=worker.pid)
        self._supervisors[record.id] = asyncio.get_running_loop().create_task(self._supervise(record.id, worker))
        self._changed()
        return True

    async def _await_ready(self, worker: WarmWorker) -> bool:
        """Wait for the worker's one-line readiness acknowledgement."""
        if worker.process.stdout is None:
            return False
        try:
            line = await asyncio.wait_for(worker.process.stdout.readline(), timeout=active_tuning().duration(Limit.WORKER_ASSIGNMENT_SECONDS))
        except asyncio.TimeoutError:
            logger.error("Worker %d did not report ready in time", worker.pid)
            return False
        if not line:
            return False
        try:
            return bool(json.loads(line.decode()).get("ready"))
        except (ValueError, UnicodeDecodeError):
            return False

    async def _supervise(self, session_id: str, worker: WarmWorker) -> None:
        """Notice a session's process ending, however it ends.

        A worker that exits on its own — a crash, an unhandled error, an OOM kill — would
        otherwise leave a record claiming to be running and a socket nothing is listening on.
        Watching the process is the only way to learn about the deaths nobody reports."""
        try:
            code = await worker.process.wait()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a wait that fails must not take the daemon with it
            code = -1
        self._pool.release()
        self._processes.pop(session_id, None)
        record = self._registry.get(session_id)
        if record is not None and record.is_live:
            # Only an unprompted death lands here as a failure; a reaped session was already
            # marked before its process was signalled.
            reason = "worker exited" if code == 0 else f"worker exited with status {code}"
            self._registry.mark(
                session_id,
                status=EXITED if code == 0 else FAILED,
                updated_at=_now(),
                exit_reason="" if code == 0 else reason,
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
        if parent is None or not parent.is_live or parent.status != RUNNING:
            return
        outcome = record.exit_reason or ("finished" if record.status == EXITED else record.status)
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
            self._registry.mark(
                descendant.id, status=EXITED, updated_at=_now(),
                exit_reason=reason or "parent session was reaped",
            )
        # Stopped together rather than one at a time: each carries its own grace period, and a
        # wide tree would otherwise take that period multiplied by its size to come down.
        await asyncio.gather(*(self._stop(record.id) for record in descendants), return_exceptions=True)
        reaped = len(descendants)
        if not skip_self and record.is_live:
            self._registry.mark(session_id, status=EXITED, updated_at=_now(), exit_reason=reason)
            await self._stop(session_id)
            reaped += 1
        self._changed()
        # After the stops, so the notice describes a session that is actually over. Only for
        # the session that was asked about: a descendant's parent is inside this same subtree
        # and is going down with it, so telling it would be writing to a corpse.
        if not skip_self:
            await self._tell_parent(self._registry.get(session_id) or record)
        return reaped

    async def _stop(self, session_id: str) -> None:
        supervisor = self._supervisors.pop(session_id, None)
        if supervisor is not None and not supervisor.done():
            supervisor.cancel()
        worker = self._processes.pop(session_id, None)
        if worker is not None:
            await self._terminate(worker)
            self._pool.release()
        _close_subscribers(session_id)
        self._unlink_socket(session_id)

    async def _terminate(self, worker: WarmWorker) -> None:
        """SIGTERM everything in the worker's process session, then SIGKILL what is left.

        The unit here has to be the process *session*, not the process group. A worker is spawned
        as a session leader, and everything it runs inherits that session id — but the bash tool
        puts each command in a group of its own, deliberately, so one job can be cancelled with its
        whole subtree. Signalling only the worker's group therefore missed every command a session
        ran: end a session that started a dev server and the port stayed held.

        So the groups are enumerated by session membership and each is signalled. The enumeration
        happens once, before anything is signalled, because a worker's children are reparented the
        moment it dies — they keep the session id, so a later sweep would still find them, but
        there is no reason to make correctness depend on that."""
        if not worker.alive:
            return
        groups = await asyncio.to_thread(session_process_groups, worker.pid)
        if not groups:
            # No listing available (an unexpected platform, `ps` missing). Falling back to the
            # worker's own group keeps the session's own death reliable, and says why it is less
            # than it should be rather than silently reaping too little.
            logger.warning("Could not enumerate the process session of worker %d; signalling its group only", worker.pid)
            with contextlib.suppress(OSError):
                groups = [os.getpgid(worker.pid)]

        def signal_groups(number: int) -> None:
            for group in groups:
                try:
                    os.killpg(group, number)
                except (ProcessLookupError, PermissionError):
                    continue

        signal_groups(signal.SIGTERM)
        if not groups:
            with contextlib.suppress(ProcessLookupError):
                worker.process.terminate()
        try:
            await asyncio.wait_for(worker.process.wait(), timeout=active_tuning().duration(Limit.SIGTERM_GRACE_SECONDS))
            return
        except asyncio.TimeoutError:
            pass
        signal_groups(signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            worker.process.kill()
        with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
            await asyncio.wait_for(worker.process.wait(), timeout=active_tuning().duration(Limit.SIGTERM_GRACE_SECONDS))

    def _unlink_socket(self, session_id: str) -> None:
        """Remove a dead session's socket file so nothing connects to a corpse."""
        with contextlib.suppress(OSError):
            session_socket_path(session_id).unlink(missing_ok=True)

    async def aclose(self) -> None:
        """Reap every live session. Called when the daemon itself is going down, so no worker
        outlives the thing that was supervising it."""
        await asyncio.gather(
            *(self.reap(record.id, reason="daemon shutting down") for record in self._registry.live()),
            return_exceptions=True,
        )
