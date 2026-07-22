"""SQLite write coordination across coroutines, threads, and backend processes.

SQLite permits only one writer. WAL improves read/write overlap, but concurrent
writers still serialize. This module makes that serialization explicit inside the
server process and across accidental multiple server processes pointing at the same
database file.

The hard constraint is that **the event loop must never block on a synchronous
lock.** The task store's async writer holds the history-database lock across an
``await`` (its transaction), so any *synchronous* acquire of the same lock on the
loop thread would deadlock the whole server — the loop can never run the code that
releases it. Therefore:

- Loop-side writers use :func:`acquire_sqlite_write_lock` / :func:`release_sqlite_write_lock`,
  which serialize on an ``asyncio.Lock`` (a waiting coroutine suspends rather than
  parking a thread-pool worker) and take the cross-process file lock in a worker.
- Off-loop writers (background threads, or sync helpers dispatched through
  ``asyncio.to_thread``) use the synchronous :func:`sqlite_write_lock` context
  manager. They may block their own thread; they must never run on the loop thread.
- ``background.db`` (background-job bookkeeping) is a *separate* database written
  only from the event loop, so it gets its own lock via :func:`background_sqlite_write_lock`.
  Sharing the history-database lock would reintroduce exactly the loop deadlock above.
"""

from __future__ import annotations

import asyncio
import fcntl
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

# history.db — the main task/session store.
_sqlite_write_lock = threading.Lock()
_sqlite_lock_path: Path | None = None

# In-process serialization for loop-side (async) history.db writers. Coroutines await
# this instead of blocking a thread-pool worker on the threading lock while they wait,
# so a burst of concurrent writers can never exhaust the default executor. Created
# lazily on the running loop.
_async_write_lock: asyncio.Lock | None = None

# background.db — background-job bookkeeping. A dedicated lock, deliberately separate
# from the history.db lock: background.db is a different database written only from the
# event loop, and sharing the history lock (which the async task store holds across an
# await) would deadlock the loop the instant a background-job write raced a task save.
_background_write_lock = threading.Lock()
_background_lock_path: Path | None = None


def configure_sqlite_lock(database_path: Path) -> None:
    """Set the cross-process lock path for the active history database."""
    global _sqlite_lock_path
    _sqlite_lock_path = database_path.with_suffix(database_path.suffix + ".lock")
    _sqlite_lock_path.parent.mkdir(parents=True, exist_ok=True)


def configure_background_sqlite_lock(database_path: Path) -> None:
    """Set the cross-process lock path for the background-job database."""
    global _background_lock_path
    _background_lock_path = database_path.with_suffix(database_path.suffix + ".lock")
    _background_lock_path.parent.mkdir(parents=True, exist_ok=True)


def _acquire_file_locks(thread_lock: threading.Lock, lock_path: Path | None):
    thread_lock.acquire()
    lock_handle = None
    try:
        if lock_path is not None:
            lock_handle = lock_path.open("a+")
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        return lock_handle
    except Exception:
        if lock_handle is not None:
            lock_handle.close()
        thread_lock.release()
        raise


def _release_file_locks(thread_lock: threading.Lock, lock_handle) -> None:
    if lock_handle is not None:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()
    thread_lock.release()


@contextmanager
def sqlite_write_lock():
    """Synchronous history.db write lock, for callers running OFF the event loop
    (background threads or sync helpers dispatched through ``asyncio.to_thread``).
    Never enter this on the event-loop thread: it blocks, and the async writer holds
    the same lock across an await, so a loop-side acquire deadlocks the whole server.
    Loop-side writers must use :func:`acquire_sqlite_write_lock` instead."""
    lock_handle = _acquire_file_locks(_sqlite_write_lock, _sqlite_lock_path)
    try:
        yield
    finally:
        _release_file_locks(_sqlite_write_lock, lock_handle)


@contextmanager
def background_sqlite_write_lock():
    """Synchronous background.db write lock — a dedicated lock kept separate from the
    history.db lock, so a background-job write on the event loop never waits on the
    task store's across-await hold. background.db writes are serialized by the single
    event-loop thread anyway; this lock only guards against a second backend process."""
    lock_handle = _acquire_file_locks(_background_write_lock, _background_lock_path)
    try:
        yield
    finally:
        _release_file_locks(_background_write_lock, lock_handle)


def run_with_sqlite_write_lock(function: Callable[..., T], *args, **kwargs) -> T:
    with sqlite_write_lock():
        return function(*args, **kwargs)


def _get_async_write_lock() -> asyncio.Lock:
    global _async_write_lock
    if _async_write_lock is None:
        _async_write_lock = asyncio.Lock()
    return _async_write_lock


class SqliteWriteToken:
    """The handle returned by :func:`acquire_sqlite_write_lock`; pass it to
    :func:`release_sqlite_write_lock`. Per-call, so overlapping acquisitions never
    clobber one shared module global."""

    __slots__ = ("_async_lock", "_file_handle", "_released")

    def __init__(self, async_lock: asyncio.Lock, file_handle) -> None:
        self._async_lock = async_lock
        self._file_handle = file_handle
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            _release_file_locks(_sqlite_write_lock, self._file_handle)
        finally:
            self._async_lock.release()


async def acquire_sqlite_write_lock() -> SqliteWriteToken:
    """Serialize an event-loop history.db writer without ever blocking the loop.

    Awaits an ``asyncio.Lock`` — a waiting coroutine suspends rather than parking a
    thread-pool worker — then takes the cross-thread/cross-process file lock in a
    worker. Because the async lock admits one writer at a time, only a single worker
    is ever used for acquisition, so a write burst cannot saturate the default
    executor. Returns a token to pass to :func:`release_sqlite_write_lock`."""
    async_lock = _get_async_write_lock()
    await async_lock.acquire()
    try:
        file_handle = await asyncio.to_thread(
            _acquire_file_locks, _sqlite_write_lock, _sqlite_lock_path
        )
    except BaseException:
        async_lock.release()
        raise
    return SqliteWriteToken(async_lock, file_handle)


def release_sqlite_write_lock(token: SqliteWriteToken) -> None:
    token.release()
