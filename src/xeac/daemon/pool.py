"""Warm workers, so spawning a session is not a Python cold start.

A worker is the same executable as the daemon, entered with a different argument. Most of
what makes it slow to start is importing the runtime — LangChain, LiteLLM, the model
clients — which is identical for every session regardless of which agent it will run. So the
pool keeps blank workers that have paid that cost already and are parked waiting for an
assignment. Claiming one is a socket write, not a process launch.

A worker is assigned exactly once and becomes that session for the rest of its life; it is
never returned to the pool and never reused. That is what keeps the isolation honest — there
is no path by which one session's state can reach another's — and it is why this is a spawn
accelerator rather than a container pool.

The pool holds a floor of warm workers and tops back up after each claim. It does not burst
above that floor, and the ceiling is not a limit on sessions: `claim` spawns a fresh worker
whenever the pool is empty, so a fan-out is never refused or queued — it simply pays a cold
start for each child past the spares. What the ceiling bounds is how many workers the daemon
will *pre-warm* into existence, so a machine already running seven sessions stops paying to
keep spares it is unlikely to use.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from dataclasses import dataclass
from typing import Optional

from xeac.base.tuning import Tunable, active_tuning

logger = logging.getLogger(__name__)

# How many blank workers to keep parked, and the point past which the daemon stops parking
# more. The floor is small because a warm worker costs real memory — two covers the common
# case of creating a session or two at a time. The ceiling counts warm *and* assigned workers
# together, so it is a bound on pre-warming rather than on how many sessions may run: a
# daemon already serving eight has better uses for the memory than more spares.
DEFAULT_WARM_FLOOR = 2
DEFAULT_WARM_CEILING = 8


@dataclass
class WarmWorker:
    """A started worker process that has not yet been told what session it is."""

    process: asyncio.subprocess.Process

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def alive(self) -> bool:
        return self.process.returncode is None


def worker_command() -> list[str]:
    """How to launch a worker.

    Deliberately a re-exec of *this* executable rather than a separate binary or a bare
    interpreter. In the packaged application the daemon runs as a signed helper inside the
    app bundle, and macOS attributes the Accessibility grant to that code identity; a worker
    launched by any other path would be a different identity and would prompt the user for
    its own grant. Re-execing the same image is what keeps one grant covering the fleet.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "worker"]
    return [sys.executable, "-m", "xeac", "worker"]


class WorkerPool:
    """Blank workers, kept warm and handed out one at a time."""

    def __init__(
        self,
        *,
        floor: int = DEFAULT_WARM_FLOOR,
        ceiling: int = DEFAULT_WARM_CEILING,
        environment: Optional[dict[str, str]] = None,
    ) -> None:
        self._floor = max(0, floor)
        self._ceiling = max(self._floor, ceiling)
        self._environment = environment
        self._warm: list[WarmWorker] = []
        self._assigned = 0
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def warm_count(self) -> int:
        return len(self._warm)

    @property
    def assigned_count(self) -> int:
        return self._assigned

    async def start(self) -> None:
        """Fill the pool to its floor. Best effort: a machine that cannot spare the processes
        still works, it just pays the cold start on each spawn."""
        await self._top_up()

    async def claim(self) -> Optional[WarmWorker]:
        """A worker to become the next session.

        Prefers a parked one; starts a fresh one when the pool is empty or has been drained by
        a burst, so a spawn is never refused just because the pool is cold."""
        async with self._lock:
            while self._warm:
                worker = self._warm.pop()
                if worker.alive:
                    self._assigned += 1
                    break
                # A parked worker that died while waiting tells us nothing useful; drop it and
                # look at the next one.
                logger.warning("Discarding dead warm worker %d", worker.pid)
            else:
                worker = await self._spawn()
                if worker is None:
                    return None
                self._assigned += 1
        # Topping up happens outside the lock so a claim never waits on a process launch.
        asyncio.get_running_loop().create_task(self._top_up())
        return worker

    def release(self) -> None:
        """Note that an assigned worker has ended. There is nothing to reclaim — a worker is
        never reused — so this only keeps the accounting honest for `daemon.status`."""
        self._assigned = max(0, self._assigned - 1)

    async def _top_up(self) -> None:
        if self._closed:
            return
        async with self._lock:
            while len(self._warm) < self._floor and (len(self._warm) + self._assigned) < self._ceiling:
                worker = await self._spawn()
                if worker is None:
                    return
                self._warm.append(worker)

    async def _spawn(self) -> Optional[WarmWorker]:
        command = worker_command()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                env={**os.environ, **(self._environment or {})},
                # A worker leads its own process group, so reaping a session can signal the
                # whole subtree — the shell commands it started included — with one killpg.
                start_new_session=True,
            )
        except OSError as error:
            logger.error("Could not start a worker: %s", error)
            return None
        return WarmWorker(process=process)

    async def aclose(self) -> None:
        """Terminate every parked worker. Assigned workers belong to their sessions and are
        reaped through the lifecycle, not here."""
        self._closed = True
        async with self._lock:
            parked, self._warm = self._warm, []
        for worker in parked:
            if worker.alive:
                with_suppress = worker.process
                try:
                    with_suppress.terminate()
                except ProcessLookupError:
                    pass
        # Concurrently: these are independent processes, and waiting them out one at a time
        # is what made quitting take tens of seconds with a warm pool.
        await asyncio.gather(
            *(self._await_exit(worker) for worker in parked), return_exceptions=True
        )

    @staticmethod
    async def _await_exit(worker: WarmWorker) -> None:
        try:
            await asyncio.wait_for(
                worker.process.wait(), timeout=active_tuning().duration(Tunable.sigterm_grace_seconds)
            )
        except (asyncio.TimeoutError, ProcessLookupError):
            with contextlib.suppress(ProcessLookupError):
                worker.process.kill()
