"""The directory of sessions: what exists, where it lives, and who may reach it.

The registry turns a session id into an address and a capability token, and remembers the
parent/child shape of a tree so a subtree can be reaped together. It deliberately does not sit
between peers: once a caller holds an address it talks to that session's socket directly.

**A session is a record, not a process.** That is the change this file carries. A session used
to *be* its worker — the record existed because the process did, and both ended together — so
a session parked on a permission prompt held a whole interpreter to wait, and a daemon restart
ended everything. Now the record is durable and the process is an activity: a session with no
worker is asleep, not gone, and the next message to it forks a new one.

Two facts follow, and they are why `status` had to split. Whether a session **exists** is
durable and is the registry's own answer (`lifecycle`). Whether it is **doing anything** is
derived from the world — does it have a process, is a turn in flight, is it waiting on a person
— and cannot be stored, because storing it means writing "working" to disk and then being
killed (`activity`). The old single field tried to be both, which is why `_public()` had to
merge `busy` in from somewhere else to answer "is it working".

Entries outlive the process they describe, and now outlive the daemon too.
"""

from __future__ import annotations

import asyncio

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator, Optional

from frank.base.identifiers import new_id
from frank.base.paths import runtime_directory, session_socket_path

# Does this session still exist? Durable, and the registry's own answer.
LIVE = "live"
ENDED = "ended"

# What is it doing right now?
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
    """The per-install key session tokens are derived from.

    A durable registry has to hand a token to a *new* worker when it wakes a slept session,
    which means the token must be recomputable rather than remembered. Deriving it stores
    nothing at rest: the table has no token column, and a database read discloses no
    capability. The precedent is `FileUrlSigner(load_or_create_secret(...))`.

    The widening this accepts is real and bounded: whatever can read this key can mint any
    session's token. The same reader could already read the daemon's own token out of the same
    0600 directory, which is a strictly stronger capability.
    """
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
    """This session's capability token, derived rather than stored.

    HMAC rather than a bare hash so the key is a key and not a prefix, and keyed on the
    session id alone so that waking a session re-derives exactly the token its creator was
    handed."""
    digest = hmac.new(_master_key(), session_id.encode(), hashlib.sha256).digest()
    return digest.hex()


@dataclass
class SessionRecord:
    """One session: its identity, where to reach it, and its place in the tree.

    Everything here is durable except `pid` and `awaiting_input`, which describe a process
    that may not exist. That division is the point: the durable half survives a restart, the
    volatile half is rebuilt from what is actually running.
    """

    id: str
    agent: str
    working_directory: str
    permission_mode: str
    # What this session's tool children may touch, resolved at creation from the machine's configuration and the agent profile and clamped against the creating session — the same once-and-immutable treatment the permission mode gets, and for the same reason.
    sandbox: dict = field(default_factory=dict)
    # Where the session's tools actually run.
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

    # Volatile: the process, if there is one right now.
    pid: int = 0
    # Set when the session is parked on a human decision, so a listing can show that it is waiting on someone rather than working.
    awaiting_input: bool = False
    # Set while a turn is in flight.
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
        """What this session is doing, derived from the world rather than remembered.

        The order is the order of interest: a session parked on a person is the fact worth
        surfacing even though a turn is technically open, and one with no process is asleep
        regardless of what it was doing when it went."""
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
        """The view a client gets.

        The capability token is never included: it is derived for the creator at `create`, and
        a listing must not hand it to anyone who can enumerate sessions."""
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
    """Every session the daemon knows about, live or finished.

    In memory, and written through to a durable store so the answer survives a restart. Both,
    rather than one or the other, because `session_for_token` runs on every control-plane call
    and must be a dictionary lookup, while "which sessions exist" must outlive the process
    holding the dictionary.

    The store is supplied rather than found — a `SessionStore` with `load_all` and `save`, the
    same shape as the other ports — so a daemon persists to SQLite and a test persists to
    nothing without either knowing about the other.
    """

    def __init__(self, store: Optional[Any] = None) -> None:
        self._sessions: dict[str, SessionRecord] = {}
        self._store = store

    def restore(self, records: list[SessionRecord]) -> None:
        """Adopt the durable records at boot.

        Every live session comes back with no process, which is exactly right: its worker died
        with the daemon, and `asleep` is what a live session with no process *is*. The first
        message to one forks it a new worker.
        """
        for record in records:
            self._sessions[record.id] = replace(record, pid=0, busy=False)

    def _persist(self, record: SessionRecord) -> None:
        """Write a record durably, from a thread that may block.

        Never call this from a coroutine. The store takes the synchronous history lock,
        which the async turn writer holds across its transaction's await — so a loop-side
        call waits on something only the loop can finish. `sqlite_write_lock` now refuses
        rather than hanging, which is what turned this from a frozen daemon into a stack
        trace. Loop-side callers use :meth:`persist_off_loop`.
        """
        if self._store is not None:
            self._store.save(record)

    async def persist_off_loop(self, record: SessionRecord) -> None:
        """Write a record durably from the event loop, without blocking it.

        For a caller that must not continue until the row exists — `create`, whose session
        is about to be handed to a worker that will look for it.
        """
        if self._store is not None:
            await asyncio.to_thread(self._persist, record)

    def _persist_wherever_we_are(self, record: SessionRecord) -> None:
        """Write a record from either side of the loop, without ever blocking it.

        `mark` is called from coroutines and from threads alike — a turn ending, a title
        arriving, a worker reporting its pid — and none of them can reasonably be asked to
        know which they are. Off the loop the write happens now; on it the write is handed to
        a thread and the caller carries on. This is bookkeeping, so a write that lands a
        moment later is correct; `create` deliberately does not use this, because the row has
        to be there before the worker looks for it.
        """
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
        """Mint a session record.

        The capability token is not minted here and not stored anywhere: it is derived from
        the session id on demand (:func:`token_for`), which is what lets a woken session hand
        its new worker the same token its creator was given."""
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
        """Every session under this one, depth-first.

        Guarded against cycles: a corrupted parent pointer must not turn reaping into an
        infinite walk, because reaping is what runs when the daemon is already shutting down.
        """
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
        """Which session a token belongs to, if any.

        The reverse of :meth:`authorize`, and the reason a control-plane call can be
        attributed at all: the daemon token says a caller may drive the daemon, while a
        session token says *which* session is driving it. Compared in constant time against
        every record — a token comparison that leaks timing is a token comparison that leaks
        the token.

        Every session is checked, not only the live ones. A token that belonged to a finished
        session must resolve to that finished session so the caller is told what happened,
        rather than falling through to "unattributed" and being treated as a person."""
        if not token:
            return None
        for record in self._sessions.values():
            if secrets.compare_digest(record.token, token):
                return record
        return None

    def authorize(self, session_id: str, token: str) -> Optional[SessionRecord]:
        """The session, if the token matches. A constant-time comparison, because this is the
        check that stands between one session and every other session on the machine."""
        record = self._sessions.get(session_id)
        if record is None or not token:
            return None
        return record if secrets.compare_digest(record.token, token) else None

    def mark(self, session_id: str, *, updated_at: str = "", **fields) -> Optional[SessionRecord]:
        """Update a record and persist it if anything durable changed.

        `pid`, `busy` and `awaiting_input` are volatile — they describe a process — so a change
        to one of those alone does not touch the store. Writing them would mean a hard kill
        leaves "working" on disk forever, which is precisely the confusion the lifecycle and
        activity split exists to prevent."""
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
