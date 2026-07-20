"""Per-agent background job runner.

Every agent owns one :class:`BackgroundJobs`. A background job is just an
``asyncio.Task`` wrapping a coroutine that returns a result string; completion is
*pushed* onto a queue by the task's done-callback, so the turn loop waits on an
event rather than polling. This replaces the old poll-driven, process-global
``TaskRegistry`` machinery.

Two properties this guarantees, both required by the runtime:

* **Isolation.** Each agent owns its jobs. A job that hangs is an unresolved
  ``asyncio.Task`` belonging to one agent — it cannot block another session, and
  it never blocks the event loop (nothing awaits it except an abort-responsive,
  timeout-bounded wait). A job that raises is captured when its result is drained
  and turned into an error payload, so an exception never propagates into the
  turn loop or crashes the server.
* **Extensibility.** Adding a new kind of background work is one row in
  :data:`BACKGROUND_PRESENTATION` plus a single ``spawn`` call — no new registry,
  manager, or dispatch layer.

Producers (``bash``, ``web_search``, and similar long-running tools) reach the
current agent's runner through :func:`current_background_jobs`, bound for the
narrow window of the producing call via :func:`bind_background_jobs`.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import signal
import weakref
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from daisy.identifiers import new_id
from daisy.core.background_store import (
    get_background_job_store,
    STATUS_COMPLETED,
    STATUS_DELIVERED,
)


# Per-kind *presentation* — how a completed job is announced to the model and how
# in-flight jobs are grouped in the turn context. This is data, not machinery:
# the concurrency handling below is identical for every kind.
BACKGROUND_PRESENTATION: dict[str, dict[str, Any]] = {
    "bash": {
        "active_context_key": "pending_bash_commands",
        "completed_event": "background_bash_completed",
        "include_result": False,
    },
    "web_search": {
        "active_context_key": "pending_web_searches",
        "completed_event": "background_web_search_completed",
        "include_result": False,
    },
    "spawn_agent": {
        "active_context_key": "pending_agents",
        "completed_event": "background_agent_completed",
        "include_result": True,
    },
}

# Identifier prefix per kind, so a job id is self-describing (e.g. ``bg-…``).
_KIND_IDENTIFIER_PREFIX: dict[str, str] = {
    "bash": "bg",
    "web_search": "search",
    "spawn_agent": "agent",
}


def background_completion_event(kind: str) -> str:
    return BACKGROUND_PRESENTATION.get(kind, {}).get("completed_event", f"{kind}_completed")


def background_include_result(kind: str) -> bool:
    return bool(BACKGROUND_PRESENTATION.get(kind, {}).get("include_result", True))


@dataclass
class _BackgroundJobRecord:
    identifier: str
    kind: str
    task: asyncio.Task
    output_path: Path | None = None
    cancel_callback: Callable[[], None] | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tool_call_identifier: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    # A detached job has an independent lifecycle (bash background=true, web_search,
    # or a spawned agent): it outlives the parent turn and Stop must NOT cancel it. A
    # non-detached job backs a foreground/synchronous call still tied to the turn.
    detached: bool = False
    # Fires when the user manually pushes a still-blocking foreground command to the
    # background: it releases the inline settle wait so the command keeps running
    # detached instead of holding the turn open (see :meth:`settle_inline`).
    detach_requested: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class BackgroundCompletion:
    """A finished job, ready to be delivered to the model."""

    kind: str
    identifier: str
    result: str
    started_at: datetime
    completed_at: datetime
    tool_call_identifier: str
    output_path: Path | None


# Weak references to every live runner, purely so the process-exit / signal
# handlers can cancel outstanding work without reintroducing a global registry
# that owns the tasks. Ownership stays with each agent.
_active_job_runners: weakref.WeakSet[BackgroundJobs] = weakref.WeakSet()


class BackgroundJobs:
    """One background-job runner, owned by a single agent runtime."""

    def __init__(self, context_id: str = "", agent_name: str = "") -> None:
        self._jobs: dict[str, _BackgroundJobRecord] = {}
        # Ids of jobs whose task has finished, pushed by the done-callback. This is
        # what makes completion event-driven instead of polled.
        self._completed_identifiers: asyncio.Queue[str] = asyncio.Queue()
        # Identity for the durable mirror. When a context is set, every job's
        # lifecycle is written to the durable store so a restart can deliver or
        # recover it; without one (e.g. in a test) durability is simply skipped.
        self._context_id = context_id
        self._agent_name = agent_name
        _active_job_runners.add(self)

    def spawn(
        self,
        kind: str,
        coroutine: Coroutine[Any, Any, str],
        *,
        identifier: str | None = None,
        output_path: Path | None = None,
        cancel_callback: Callable[[], None] | None = None,
        arguments: dict[str, Any] | None = None,
        tool_call_identifier: str = "",
        detached: bool = False,
    ) -> str:
        """Start ``coroutine`` as a background job and return its identifier.

        ``arguments`` carries what a restart needs to re-issue an idempotent job (e.g.
        a search query or a parse target); it is persisted, never used live.
        ``detached`` marks work with an independent lifecycle, which a Stop must
        leave running (see :meth:`cancel_foreground`)."""
        if identifier is None:
            identifier = new_id(_KIND_IDENTIFIER_PREFIX.get(kind, kind))
        task = asyncio.create_task(coroutine)
        self._jobs[identifier] = _BackgroundJobRecord(
            identifier=identifier,
            kind=kind,
            task=task,
            output_path=output_path,
            cancel_callback=cancel_callback,
            tool_call_identifier=tool_call_identifier,
            arguments=arguments or {},
            detached=detached,
        )
        if self._context_id:
            get_background_job_store().record_started(
                job_id=identifier,
                context_id=self._context_id,
                agent_name=self._agent_name,
                kind=kind,
                arguments=arguments or {},
                tool_call_id=tool_call_identifier,
            )
        task.add_done_callback(
            lambda _task, job_identifier=identifier: self._on_task_done(job_identifier)
        )
        return identifier

    def _on_task_done(self, identifier: str) -> None:
        """Runs the instant a job's task finishes. Persist the result immediately —
        before it is delivered — so a crash between completion and delivery cannot
        lose it; then signal the (event-driven) waiters."""
        record = self._jobs.get(identifier)
        if record is not None and self._context_id:
            result = self._result_string(record)
            get_background_job_store().record_finished(identifier, result, status=STATUS_COMPLETED)
        self._completed_identifiers.put_nowait(identifier)

    def bind_tool_call(self, identifier: str, tool_call_identifier: str) -> None:
        """Correlate a job with the tool call that started it, so its eventual
        completion message can reference the originating call."""
        record = self._jobs.get(identifier)
        if record is not None:
            record.tool_call_identifier = tool_call_identifier

    def add_done_callback(self, identifier: str, callback: Callable[[str], None]) -> bool:
        """Attach a side-effect callback (e.g. releasing a filesystem lease) that
        fires with the job identifier when its task finishes."""
        record = self._jobs.get(identifier)
        if record is None:
            return False
        record.task.add_done_callback(
            lambda _task, job_identifier=identifier: callback(job_identifier)
        )
        return True

    def has_pending(self) -> bool:
        """True while any job has not yet been drained (running or finished-but-undelivered)."""
        return bool(self._jobs)

    def has_completed_undelivered(self) -> bool:
        """True when at least one job has finished but its result has not yet been
        drained — i.e. there is something for an autonomous wake to deliver."""
        return any(record.task.done() for record in self._jobs.values())

    def active_count(self) -> int:
        return sum(1 for record in self._jobs.values() if not record.task.done())

    def active_snapshots(self) -> list[dict[str, Any]]:
        """Current in-memory jobs, for UI status surfaces."""
        snapshots: list[dict[str, Any]] = []
        for record in self._jobs.values():
            if record.task.done():
                continue
            snapshots.append({
                "job_id": record.identifier,
                "kind": record.kind,
                "tool_call_id": record.tool_call_identifier,
                "arguments": dict(record.arguments),
                "started_at": record.started_at.isoformat(),
                "detached": record.detached,
            })
        return snapshots

    def active_by_context_key(self) -> dict[str, list[str]]:
        """In-flight job identifiers grouped by their turn-context key, e.g.
        ``{"pending_bash_commands": ["bg-…"]}``. Empty groups are omitted."""
        grouped: dict[str, list[str]] = {}
        for record in self._jobs.values():
            if record.task.done():
                continue
            context_key = BACKGROUND_PRESENTATION.get(record.kind, {}).get(
                "active_context_key", f"pending_{record.kind}"
            )
            grouped.setdefault(context_key, []).append(record.identifier)
        return grouped

    async def wait_for_completion(
        self,
        *wake_events: asyncio.Event,
        timeout: float | None = None,
    ) -> None:
        """Block until at least one job finishes or a wake event fires. Event-driven
        (awaits the completion queue) — never polls. With no ``timeout`` it blocks
        indefinitely, which is what the autonomous-resume pump wants: sleep at zero
        cost until a result lands, then deliver it."""
        if not self._jobs or not self._completed_identifiers.empty():
            return
        completion_getter: asyncio.Future = asyncio.ensure_future(self._completed_identifiers.get())
        waiters: list[asyncio.Future] = [completion_getter]
        for wake_event in wake_events:
            waiters.append(asyncio.ensure_future(wake_event.wait()))
        try:
            await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
        # If we pulled an id off the queue, put it back so drain_completed sees it;
        # if we merely woke for abort/steering/timeout, the getter is cancelled and
        # no id is lost.
        if completion_getter.done() and not completion_getter.cancelled() and completion_getter.exception() is None:
            self._completed_identifiers.put_nowait(completion_getter.result())

    async def settle_inline(self, identifier: str, timeout: float) -> BackgroundCompletion | None:
        """Give a just-spawned job a brief window to finish *inline*.

        If it completes within ``timeout`` it is drained here — removed from tracking
        and marked delivered in the durable store — and its completion is returned so
        the caller can hand the result straight back (this is the "fast commands
        return directly" path for bash). Crucially, a job settled this way never
        becomes an orphaned pending job, so it can never trigger a spurious autonomous
        wake once the model has already moved on. If the window closes with the job
        still running, ``None`` is returned and it stays backgrounded exactly as
        before, to be delivered by the usual completion path.
        """
        record = self._jobs.get(identifier)
        if record is None:
            return None
        # Race the job's completion against the ceiling and a manual background
        # request. Whichever fires first wins: a finished job is drained and returned
        # inline; a timeout or a user backgrounding leaves the job running (the latter
        # marks it detached below) so the turn continues with a "started" placeholder.
        task_waiter = asyncio.ensure_future(asyncio.shield(record.task))
        detach_waiter = asyncio.ensure_future(record.detach_requested.wait())
        try:
            await asyncio.wait(
                {task_waiter, detach_waiter},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            detach_waiter.cancel()
            if not task_waiter.done():
                task_waiter.cancel()
            elif not task_waiter.cancelled():
                # The task finished by raising; consume the exception here so it is not
                # reported as never-retrieved. It is re-captured into an error payload
                # when the completion is built below.
                task_waiter.exception()
        if record.detach_requested.is_set():
            # Backgrounded by the user: mark it detached so a Stop leaves it running,
            # and hand back None so bash returns the "started" placeholder.
            record.detached = True
            return None
        if not record.task.done():
            return None
        record = self._jobs.pop(identifier, None)
        if record is None:
            return None
        # The done-callback already enqueued this id on `_completed_identifiers`; we
        # leave it there because `drain_completed` skips ids no longer in `_jobs`
        # (its own dedup), so the stale signal is harmless. Removing from `_jobs` is
        # what makes `has_pending()`/`has_completed_undelivered()` forget the job, so
        # no resume pump wakes for it.
        if self._context_id:
            get_background_job_store().mark_delivered(identifier)
        return self._build_completion(record)

    def drain_completed(self) -> list[BackgroundCompletion]:
        """Remove and return every finished job that has signalled completion."""
        signalled: list[str] = []
        while not self._completed_identifiers.empty():
            signalled.append(self._completed_identifiers.get_nowait())
        completions: list[BackgroundCompletion] = []
        for identifier in signalled:
            record = self._jobs.pop(identifier, None)
            if record is None:  # already drained (deduplicates repeated signals)
                continue
            completions.append(self._build_completion(record))
            if self._context_id:
                get_background_job_store().mark_delivered(identifier)
        return completions

    def cancel_all(self) -> None:
        for record in list(self._jobs.values()):
            if record.cancel_callback is not None:
                try:
                    record.cancel_callback()
                except Exception:
                    pass
            record.task.cancel()
        self._jobs.clear()

    def cancel_foreground(self) -> None:
        """Cancel only jobs tied to the current turn (a synchronous bash still
        running, an inline command). Detached jobs the model explicitly
        backgrounded are left running — a Stop ends the turn, not the work the
        user deliberately sent to the background."""
        for identifier, record in list(self._jobs.items()):
            if record.detached:
                continue
            if record.cancel_callback is not None:
                try:
                    record.cancel_callback()
                except Exception:
                    pass
            record.task.cancel()
            if self._context_id:
                result = json.dumps({"code": f"{record.kind}_cancelled", "task_identifier": record.identifier})
                get_background_job_store().record_finished(identifier, result, status=STATUS_DELIVERED)
            self._jobs.pop(identifier, None)

    def cancel_by_tool_call(self, tool_call_identifier: str) -> bool:
        """Cancel the live background job associated with a tool card."""
        for identifier, record in list(self._jobs.items()):
            if record.tool_call_identifier != tool_call_identifier:
                continue
            if record.task.done():
                return False
            if record.cancel_callback is not None:
                try:
                    record.cancel_callback()
                except Exception:
                    pass
            record.task.cancel()
            if self._context_id:
                result = json.dumps({"code": f"{record.kind}_cancelled", "task_identifier": record.identifier})
                get_background_job_store().record_finished(identifier, result, status=STATUS_DELIVERED)
            self._jobs.pop(identifier, None)
            return True
        return False

    def cancel_by_identifier(self, identifier: str, *, kind: str | None = None) -> bool:
        """Cancel one live job by its public handle.

        ``kind`` optionally constrains the match so a caller that owns one class of
        handle cannot accidentally cancel another background-work type.
        """
        record = self._jobs.get(identifier)
        if record is None or record.task.done() or (kind is not None and record.kind != kind):
            return False
        if record.cancel_callback is not None:
            try:
                record.cancel_callback()
            except Exception:
                pass
        record.task.cancel()
        if self._context_id:
            result = json.dumps({"code": f"{record.kind}_cancelled", "task_identifier": record.identifier})
            get_background_job_store().record_finished(identifier, result, status=STATUS_DELIVERED)
        self._jobs.pop(identifier, None)
        return True

    def request_background(self, tool_call_identifier: str) -> bool:
        """Push a still-running foreground job to the background on the user's behalf.

        Finds the live, not-yet-detached job started by ``tool_call_identifier`` and
        signals it: its inline settle wait releases at once (the command keeps running
        detached, so a Stop no longer kills it and the turn continues with a "started"
        placeholder). Returns True when such a job was found and signalled."""
        for record in self._jobs.values():
            if record.tool_call_identifier != tool_call_identifier:
                continue
            if record.task.done() or record.detached or record.detach_requested.is_set():
                return False
            record.detach_requested.set()
            return True
        return False

    def _result_string(self, record: _BackgroundJobRecord) -> str:
        """The finished task's result as a string. A cancelled or failed job becomes
        an error payload rather than a raised exception, so it is delivered like any
        other result and never escapes into the caller."""
        try:
            result: Any = record.task.result()
        except asyncio.CancelledError:
            result = json.dumps({"code": f"{record.kind}_cancelled", "task_identifier": record.identifier})
        except Exception as exception:
            result = json.dumps({
                "code": f"{record.kind}_error",
                "task_identifier": record.identifier,
                "message": str(exception),
            })
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False)
        return result

    def _build_completion(self, record: _BackgroundJobRecord) -> BackgroundCompletion:
        return BackgroundCompletion(
            kind=record.kind,
            identifier=record.identifier,
            result=self._result_string(record),
            started_at=record.started_at,
            completed_at=datetime.now(timezone.utc),
            tool_call_identifier=record.tool_call_identifier,
            output_path=record.output_path,
        )


def cancel_all_background_jobs() -> None:
    """Cancel outstanding jobs across every live runner. Used only by the
    process-exit and termination-signal handlers — normal teardown is per-agent
    via :meth:`BackgroundJobs.cancel_all`."""
    for runner in list(_active_job_runners):
        runner.cancel_all()


def reap_orphaned_processes() -> int:
    """Kill process groups left behind by a previous, unclean shutdown.

    Catchable termination (SIGTERM/SIGINT/SIGHUP/normal exit) kills every tracked
    process group directly. A SIGKILL or a hard crash cannot run any handler, so a
    long background shell subtree (a dev server, a watcher) can survive as an
    orphan. Each job records its process-group id durably when it starts; on the
    next startup this kills any group still marked running, so orphans never
    accumulate. Returns how many groups were signalled."""
    killed = 0
    for process_group in get_background_job_store().orphaned_process_groups():
        try:
            os.killpg(process_group, signal.SIGKILL)
            killed += 1
        except ProcessLookupError:
            # Already gone — the common case (the child died with the crash).
            continue
        except OSError as error:
            logging.getLogger(__name__).warning(
                "Could not reap orphaned process group %s: %s", process_group, error
            )
    return killed


# Ambient binding for background-producing tools: bind the current runner during dispatch.
# Background-producing tools (bash, web_search, …) are module-level functions
# with LLM-facing signatures, so they cannot take the runner as a parameter. The
# dispatcher binds the current agent's runner for the narrow duration of the
# producing call; the producer reads it through current_background_jobs().

_current_background_jobs: contextvars.ContextVar[BackgroundJobs | None] = contextvars.ContextVar(
    "current_background_jobs", default=None
)


def bind_background_jobs(jobs: BackgroundJobs) -> contextvars.Token:
    return _current_background_jobs.set(jobs)


def unbind_background_jobs(token: contextvars.Token) -> None:
    _current_background_jobs.reset(token)


def current_background_jobs() -> BackgroundJobs:
    jobs = _current_background_jobs.get()
    if jobs is None:
        raise RuntimeError("No background job runner is bound to the current context.")
    return jobs


# The tool call currently executing, bound by the dispatcher alongside the job
# runner. A background-producing tool reads it so the job it spawns is correlated
# with its originating tool call from the very start — which is what lets a user
# background a still-blocking foreground command by that tool-call id, before the
# post-return bind_tool_call would otherwise set it.
_current_tool_call_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_tool_call_id", default=""
)


def bind_tool_call_id(tool_call_identifier: str) -> contextvars.Token:
    return _current_tool_call_id.set(tool_call_identifier)


def unbind_tool_call_id(token: contextvars.Token) -> None:
    _current_tool_call_id.reset(token)


def current_tool_call_id() -> str:
    return _current_tool_call_id.get()
