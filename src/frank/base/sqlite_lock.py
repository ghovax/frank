"""SQLite write coordination across coroutines, threads, and backend processes."""

from __future__ import annotations

import asyncio
import fcntl
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

_Result = TypeVar("_Result")

# history.db — the main task/session store.
_sqlite_write_lock = threading.Lock()
_sqlite_lock_path: Path | None = None

# In-process serialization for loop-side (async) history.db writers.
_async_write_lock: asyncio.Lock | None = None

# background.db — background-job bookkeeping.
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


def _refuse_on_event_loop() -> None:
    """Raise if this thread is running an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        "sqlite_write_lock() was entered on the event-loop thread, which would deadlock "
        "the daemon. Await acquire_sqlite_write_lock()/release_sqlite_write_lock() here, "
        "or move this write off the loop with asyncio.to_thread()."
    )


@contextmanager
def sqlite_write_lock():
    """Synchronous history.db write lock, for callers running OFF the event loop (background threads or sync helpers dispatched through ``asyncio.to_thread``)."""
    _refuse_on_event_loop()
    lock_handle = _acquire_file_locks(_sqlite_write_lock, _sqlite_lock_path)
    try:
        yield
    finally:
        _release_file_locks(_sqlite_write_lock, lock_handle)


@contextmanager
def background_sqlite_write_lock():
    """Synchronous background.db write lock — a dedicated lock kept separate from the history.db lock, so a background-job write on the event loop never waits on the task store's across-await hold. background.db writes are serialized by the single event-loop thread anyway; this lock only guards against a second backend process."""
    lock_handle = _acquire_file_locks(_background_write_lock, _background_lock_path)
    try:
        yield
    finally:
        _release_file_locks(_background_write_lock, lock_handle)


def _get_async_write_lock() -> asyncio.Lock:
    global _async_write_lock
    if _async_write_lock is None:
        _async_write_lock = asyncio.Lock()
    return _async_write_lock


class SqliteWriteToken:
    """The handle returned by :func:`acquire_sqlite_write_lock`; pass it to :func:`release_sqlite_write_lock`."""

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
    """Serialize an event-loop history.db writer without ever blocking the loop."""
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
