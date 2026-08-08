"""Session-scoped filesystem mutation leases, so concurrent sessions' writes have a coordination point."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


class FileLeaseConflict(RuntimeError):
    def __init__(self, message: str, *, owner_session_id: str = "", path: str = ""):
        super().__init__(message)
        self.owner_session_id = owner_session_id
        self.path = path


@dataclass
class FileLease:
    token: str
    owner_session_id: str
    scope: str
    path: str
    working_directory: str
    description: str
    acquired_at: float


class FileLeaseManager:
    def __init__(
        self, lock_directory: Path | None = None, on_change: Callable[[], None] | None = None
    ):
        self._condition = threading.Condition()
        self._leases: dict[str, FileLease] = {}
        self._lock_handles: dict[str, list] = {}
        self._lock_directory = lock_directory or (
            Path(tempfile.gettempdir()) / "langmesh-file-leases"
        )
        self._lock_directory.mkdir(parents=True, exist_ok=True)
        self._on_change = on_change

    async def acquire(
        self,
        *,
        owner_session_id: str,
        scope: str,
        path: str,
        working_directory: str,
        description: str,
        timeout: float = 2.0,
    ) -> str:
        return await asyncio.to_thread(
            self._acquire_sync,
            owner_session_id=owner_session_id,
            scope=scope,
            path=path,
            working_directory=working_directory,
            description=description,
            timeout=timeout,
        )

    def release(self, token: str) -> None:
        with self._condition:
            self._leases.pop(token, None)
            handles = self._lock_handles.pop(token, [])
            for handle in reversed(handles):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            self._condition.notify_all()
        self._changed()

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change()

    def active_for_session(self, session_id: str) -> list[dict]:
        with self._condition:
            return [
                self._lease_payload(lease)
                for lease in self._leases.values()
                if lease.owner_session_id == session_id
            ]

    def active(self) -> list[dict]:
        with self._condition:
            return [self._lease_payload(lease) for lease in self._leases.values()]

    def _acquire_sync(
        self,
        *,
        owner_session_id: str,
        scope: str,
        path: str,
        working_directory: str,
        description: str,
        timeout: float,
    ) -> str:
        if scope not in ("file", "worktree"):
            raise ValueError("lease scope must be 'file' or 'worktree'.")

        token = str(uuid.uuid4())
        deadline = time.monotonic() + timeout
        lease = FileLease(
            token=token,
            owner_session_id=owner_session_id,
            scope=scope,
            path=path,
            working_directory=working_directory,
            description=description,
            acquired_at=time.time(),
        )

        handles: list = []
        while True:
            with self._condition:
                conflict = self._first_conflict(lease)
                if conflict is None:
                    try:
                        handles = self._acquire_os_locks(lease)
                    except BlockingIOError:
                        conflict = None
                    else:
                        self._leases[token] = lease
                        self._lock_handles[token] = handles
                        self._condition.notify_all()
                        self._changed()
                        return token

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if handles:
                        for handle in reversed(handles):
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                            handle.close()
                    if conflict is not None:
                        raise FileLeaseConflict(
                            (
                                f"Filesystem is busy: session {conflict.owner_session_id} holds a {conflict.scope} lease on {conflict.path}. Try again after that turn finishes."
                            ),
                            owner_session_id=conflict.owner_session_id,
                            path=conflict.path,
                        )
                    raise FileLeaseConflict(
                        "Filesystem is busy in another backend process. Try again shortly."
                    )
                self._condition.wait(timeout=min(0.1, remaining))

    def _first_conflict(self, candidate: FileLease) -> FileLease | None:
        for lease in self._leases.values():
            if self._leases_conflict(candidate, lease):
                return lease
        return None

    def _leases_conflict(self, left: FileLease, right: FileLease) -> bool:
        if left.scope == "file" and right.scope == "file":
            return left.path == right.path
        if left.scope == "worktree" and right.scope == "worktree":
            return self._paths_overlap(left.path, right.path)
        if left.scope == "file" and right.scope == "worktree":
            return self._is_relative_to(left.path, right.path)
        if left.scope == "worktree" and right.scope == "file":
            return self._is_relative_to(right.path, left.path)
        return False

    def _acquire_os_locks(self, lease: FileLease) -> list:
        handles = []
        try:
            if lease.scope == "file":
                handles.append(self._lock_os_key("worktree", lease.working_directory, shared=True))
                handles.append(self._lock_os_key("file", lease.path, shared=False))
            else:
                handles.append(self._lock_os_key("worktree", lease.path, shared=False))
            return handles
        except Exception:
            for handle in reversed(handles):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            raise

    def _lock_os_key(self, scope: str, path: str, *, shared: bool):
        key = hashlib.sha256(f"{scope}:{path}".encode()).hexdigest()
        handle = (self._lock_directory / f"{key}.lock").open("a+")
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(handle.fileno(), operation | fcntl.LOCK_NB)
        handle.seek(0)
        handle.truncate()
        handle.write(path)
        handle.flush()
        return handle

    def _lease_payload(self, lease: FileLease) -> dict:
        return {
            "owner_session_id": lease.owner_session_id,
            "scope": lease.scope,
            "path": lease.path,
            "working_directory": lease.working_directory,
            "description": lease.description,
            "acquired_at": lease.acquired_at,
        }

    def _paths_overlap(self, left: str, right: str) -> bool:
        return self._is_relative_to(left, right) or self._is_relative_to(right, left)

    def _is_relative_to(self, path: str, parent: str) -> bool:
        try:
            path_obj = Path(path)
            parent_obj = Path(parent)
            return path_obj == parent_obj or path_obj.is_relative_to(parent_obj)
        except Exception:
            return path == parent
