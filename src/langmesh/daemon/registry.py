"""The directory of sessions: what exists, where it lives, who may reach it, and how the tree is shaped."""

from __future__ import annotations

import asyncio

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator, Optional

from langmesh.base.identifiers import new_id
from langmesh.base.paths import runtime_directory, session_socket_path

# Does this session still exist? Durable, and the registry's own answer.
LIVE = "live"
ENDED = "ended"

# What it is doing right now, derived on read rather than stored, since a stored "working" survives the kill that ended it.
WORKING = "working"   # a turn is in flight
WAITING = "waiting"   # parked on a human decision
IDLE = "idle"         # has a worker, doing nothing
ASLEEP = "asleep"     # has no worker; the next message forks one
STARTING = "starting"  # forked, not yet accepting connections
ENDED_ACTIVITY = "ended"

# How a session finished, for the record that outlives it.
EXITED = "exited"
FAILED = "failed"

TERMINAL_OUTCOMES = frozenset({EXITED, FAILED})


def _master_key() -> bytes:
    """The per-install key session tokens are derived from, so a woken session's token is recomputable rather than remembered."""
    path = runtime_directory() / "session_master_key"
    if path.exists():
        existing = path.read_bytes()
        if existing:
            return existing
    key = os.urandom(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def token_for(session_id: str) -> str:
    """This session's capability token, derived by HMAC from its id so waking re-derives exactly the same one."""
    digest = hmac.new(_master_key(), session_id.encode(), hashlib.sha256).digest()
    return digest.hex()


@dataclass
class SessionRecord:
    """One session: its identity, where to reach it, and its place in the tree, durable except for what describes a process."""

    id: str
    agent: str
    working_directory: str
    permission_mode: str
    # What this session's tool children may touch, resolved and clamped once at creation.
    sandbox: dict = field(default_factory=dict)
    # Where the session's tools actually run, which a worktree strategy decides once when the session is created.
    runtime_working_directory: str = ""
    workspace_id: str = ""
    parent: str = ""
    title: str = ""
    created_at: str = ""
    updated_at: str = ""

    # Durable: does this session still exist?
    lifecycle: str = LIVE
    # How it finished, and why. Empty while it is live, and for an ordinary exit.
    outcome: str = ""
    exit_reason: str = ""

    # The process, if there is one right now; zero means asleep and the next message forks a worker.
    pid: int = 0
    # Set when the session is parked on a decision, rebuilt on restart from the turn store where the suspension lives.
    awaiting_input: bool = False
    # Set while a turn is in flight, purely in memory because a stored value outlives the kill that ended it.
    busy: bool = False

    @property
    def token(self) -> str:
        """This session's capability token, derived from its id. Never stored."""
        return token_for(self.id)

    @property
    def socket_path(self) -> Path:
        return session_socket_path(self.id)

    @property
    def is_live(self) -> bool:
        return self.lifecycle == LIVE

    @property
    def asleep(self) -> bool:
        """Live, but with no process. The next message to it forks one."""
        return self.is_live and self.pid == 0

    @property
    def activity(self) -> str:
        """What this session is doing, derived from the world, ordered so the most interesting fact wins."""
        if not self.is_live:
            return ENDED_ACTIVITY
        if self.awaiting_input:
            return WAITING
        if self.pid == 0:
            return ASLEEP
        if self.busy:
            return WORKING
        return IDLE

    def public(self) -> dict:
        """The view a client gets, never including the capability token that a listing must not hand out."""
        return {
            "id": self.id,
            "agent": self.agent,
            "parent": self.parent,
            "lifecycle": self.lifecycle,
            "activity": self.activity,
            "outcome": self.outcome,
            "awaiting_input": self.awaiting_input,
            "title": self.title,
            "working_directory": self.working_directory,
            "runtime_working_directory": self.runtime_working_directory,
            "workspace_id": self.workspace_id,
            "permission_mode": self.permission_mode,
            "sandbox": self.sandbox,
            "pid": self.pid,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "exit_reason": self.exit_reason,
        }


class SessionRegistry:
    """Every session the daemon knows, in memory and written through so the answer survives a restart."""

    def __init__(self, store: Optional[Any] = None) -> None:
        self._sessions: dict[str, SessionRecord] = {}
        self._store = store

    def restore(self, records: list[SessionRecord]) -> None:
        """Adopt the durable records at boot, each with no process, which is what a live session asleep is."""
        for record in records:
            self._sessions[record.id] = replace(record, pid=0, busy=False)

    def _persist(self, record: SessionRecord) -> None:
        """Write a record durably from a thread that may block, never from a coroutine."""
        if self._store is not None:
            self._store.save(record)

    async def persist_off_loop(self, record: SessionRecord) -> None:
        """Write a record durably from the event loop without blocking it, for a caller that must not continue until the row exists."""
        if self._store is not None:
            await asyncio.to_thread(self._persist, record)

    def _persist_wherever_we_are(self, record: SessionRecord) -> None:
        """Write a record from either side of the loop, since `mark` is called from coroutines and threads alike."""
        if self._store is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._persist(record)
            return
        loop.create_task(asyncio.to_thread(self._persist, record))

    def create(
        self,
        *,
        agent: str,
        working_directory: str,
        permission_mode: str,
        sandbox: Optional[dict] = None,
        workspace_id: str = "",
        parent: str = "",
        title: str = "",
        created_at: str = "",
        runtime_working_directory: str = "",
    ) -> SessionRecord:
        """Mint a session record, with the capability token derived on demand rather than stored."""
        identifier = new_id("session")
        record = SessionRecord(
            id=identifier,
            agent=agent,
            working_directory=working_directory,
            runtime_working_directory=runtime_working_directory or working_directory,
            permission_mode=permission_mode,
            sandbox=dict(sandbox or {}),
            workspace_id=workspace_id,
            parent=parent,
            title=title,
            created_at=created_at,
            updated_at=created_at,
        )
        self._sessions[identifier] = record
        return record

    def get(self, session_id: str) -> Optional[SessionRecord]:
        return self._sessions.get(session_id)

    def all(self) -> list[SessionRecord]:
        return list(self._sessions.values())

    def live(self) -> list[SessionRecord]:
        return [record for record in self._sessions.values() if record.is_live]

    def running(self) -> list[SessionRecord]:
        """Live sessions that currently have a process. The ones a shutdown must reap."""
        return [record for record in self._sessions.values() if record.is_live and record.pid]

    def children_of(self, session_id: str) -> list[SessionRecord]:
        return [record for record in self._sessions.values() if record.parent == session_id]

    def descendants_of(self, session_id: str) -> Iterator[SessionRecord]:
        """Every session under this one, depth-first and guarded against cycles."""
        seen: set[str] = set()
        frontier = [session_id]
        while frontier:
            current = frontier.pop()
            for child in self.children_of(current):
                if child.id in seen:
                    continue
                seen.add(child.id)
                frontier.append(child.id)
                yield child

    def session_for_token(self, token: str) -> Optional[SessionRecord]:
        """Which session a token belongs to, which is what lets a control-plane call be attributed to a session."""
        if not token:
            return None
        for record in self._sessions.values():
            if secrets.compare_digest(record.token, token):
                return record
        return None

    def authorize(self, session_id: str, token: str) -> Optional[SessionRecord]:
        """The session, if the token matches, compared in constant time."""
        record = self._sessions.get(session_id)
        if record is None or not token:
            return None
        return record if secrets.compare_digest(record.token, token) else None

    def mark(self, session_id: str, *, updated_at: str = "", **fields) -> Optional[SessionRecord]:
        """Update a record and persist it if anything durable changed, leaving the volatile fields out of the store."""
        record = self._sessions.get(session_id)
        if record is None:
            return None
        if updated_at:
            record.updated_at = updated_at
        durable_changed = bool(updated_at)
        for name, value in fields.items():
            if not hasattr(record, name):
                continue
            setattr(record, name, value)
            if name not in _VOLATILE_FIELDS:
                durable_changed = True
        if durable_changed:
            self._persist_wherever_we_are(record)
        return record

    def end(
        self, session_id: str, *, outcome: str = EXITED, reason: str = "", updated_at: str = "",
    ) -> Optional[SessionRecord]:
        """Mark a session finished. The one transition out of `live`, in one place."""
        return self.mark(
            session_id,
            lifecycle=ENDED,
            outcome=outcome,
            exit_reason=reason,
            pid=0,
            busy=False,
            awaiting_input=False,
            updated_at=updated_at,
        )

    def sleep(self, session_id: str, *, updated_at: str = "") -> Optional[SessionRecord]:
        """Note that a live session no longer has a process. It stays live; it is now asleep."""
        return self.mark(session_id, pid=0, busy=False, updated_at=updated_at)

    def forget(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        if self._store is not None:
            self._store.delete(session_id)


# Fields that describe a process rather than a session, and are therefore never written down.
_VOLATILE_FIELDS = frozenset({"pid", "busy", "awaiting_input"})
