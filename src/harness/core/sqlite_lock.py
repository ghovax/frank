"""SQLite write coordination across threads and backend processes.

SQLite permits only one writer. WAL improves read/write overlap, but concurrent
writers still serialize. This lock makes that serialization explicit inside the
server process and across accidental multiple server processes that point at the
same database file.
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

_sqlite_write_lock = threading.Lock()
_sqlite_lock_path: Path | None = None
_sqlite_lock_handle = None


def configure_sqlite_lock(database_path: Path) -> None:
    """Set the cross-process lock path for the active SQLite database."""
    global _sqlite_lock_path
    _sqlite_lock_path = database_path.with_suffix(database_path.suffix + ".lock")
    _sqlite_lock_path.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def sqlite_write_lock():
    lock_handle = _acquire_sqlite_locks()
    try:
        yield
    finally:
        _release_sqlite_locks(lock_handle)


def _acquire_sqlite_locks():
    _sqlite_write_lock.acquire()
    lock_handle = None
    try:
        if _sqlite_lock_path is not None:
            lock_handle = _sqlite_lock_path.open("a+")
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        return lock_handle
    except Exception:
        if lock_handle is not None:
            lock_handle.close()
        _sqlite_write_lock.release()
        raise


def _release_sqlite_locks(lock_handle) -> None:
    if lock_handle is not None:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()
    _sqlite_write_lock.release()


def run_with_sqlite_write_lock(function: Callable[..., T], *args, **kwargs) -> T:
    with sqlite_write_lock():
        return function(*args, **kwargs)


async def acquire_sqlite_write_lock() -> None:
    global _sqlite_lock_handle
    _sqlite_lock_handle = await asyncio.to_thread(_acquire_sqlite_locks)


def release_sqlite_write_lock() -> None:
    global _sqlite_lock_handle
    lock_handle = _sqlite_lock_handle
    _sqlite_lock_handle = None
    _release_sqlite_locks(lock_handle)
