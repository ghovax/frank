"""The directory of sessions: what exists, where it lives, and who may reach it.

A session is one worker process. The registry is what turns a session id into an address
and a capability token, and what remembers the parent/child shape of a tree so a subtree can
be reaped together. It deliberately does not sit between peers: once a caller holds an
address it talks to that session's socket directly.

Entries outlive the process they describe. A reaped session stays here as a terminal record
until the daemon restarts, so `daisy ps` can still explain what happened to it, and so a
child's death is attributable to the parent that took it down.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from daisy.base.identifiers import new_id
from daisy.base.paths import session_socket_path

# Session lifecycle. `starting` covers the window between minting the record and the worker
# reporting that its socket is accepting connections; a client that races ahead of that gets
# a clear "not ready yet" rather than a connection refused.
STARTING = "starting"
RUNNING = "running"
EXITED = "exited"
FAILED = "failed"

TERMINAL_STATES = frozenset({EXITED, FAILED})


@dataclass
class SessionRecord:
    """One session: its identity, where to reach it, and its place in the tree."""

    id: str
    agent: str
    working_directory: str
    permission_mode: str
    # What this session's tool children may touch, resolved at creation from the machine's
    # configuration and the agent profile and clamped against the creating session — the same
    # once-and-immutable treatment the permission mode gets, and for the same reason.
    sandbox: dict = field(default_factory=dict)
    # Where the session's tools actually run. Equal to `working_directory` unless the
    # workspace strategy put the session in its own worktree or branch, which is decided once
    # when the session is created — the same moment everything else about it is fixed.
    runtime_working_directory: str = ""
    project_id: str = ""
    parent: str = ""
    token: str = ""
    pid: int = 0
    status: str = STARTING
    # Set when the session is parked on a human decision, so `ps` can show that it is waiting
    # on someone rather than working.
    awaiting_input: bool = False
    title: str = ""
    created_at: str = ""
    updated_at: str = ""
    # Why the session ended, when it ended for a reason worth reporting (a crash, a parent
    # being reaped). Empty for an ordinary exit.
    exit_reason: str = ""

    @property
    def socket_path(self) -> Path:
        return session_socket_path(self.id)

    @property
    def is_live(self) -> bool:
        return self.status not in TERMINAL_STATES

    def public(self) -> dict:
        """The view a client gets. The capability token is never included: it is handed to the
        creator once, at `create`, and a listing must not leak it to anyone who can enumerate
        sessions."""
        return {
            "id": self.id,
            "agent": self.agent,
            "parent": self.parent,
            "status": self.status,
            "awaiting_input": self.awaiting_input,
            "title": self.title,
            "working_directory": self.working_directory,
            "runtime_working_directory": self.runtime_working_directory,
            "project_id": self.project_id,
            "permission_mode": self.permission_mode,
            "sandbox": self.sandbox,
            "pid": self.pid,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "exit_reason": self.exit_reason,
        }


class SessionRegistry:
    """Every session the daemon knows about, live or finished."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionRecord] = {}

    def create(
        self,
        *,
        agent: str,
        working_directory: str,
        permission_mode: str,
        sandbox: Optional[dict] = None,
        project_id: str = "",
        parent: str = "",
        title: str = "",
        created_at: str = "",
    ) -> SessionRecord:
        """Mint a session record and its capability token.

        The token is what proves a caller may drive this session. It is generated here and
        returned to the creator exactly once; nothing else can recover it, which is what makes
        holding a session's handle meaningfully different from knowing its id."""
        identifier = new_id("session")
        record = SessionRecord(
            id=identifier,
            agent=agent,
            working_directory=working_directory,
            permission_mode=permission_mode,
            sandbox=dict(sandbox or {}),
            project_id=project_id,
            parent=parent,
            token=secrets.token_urlsafe(32),
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
        every live record — the set is small, and a token comparison that leaks timing is a
        token comparison that leaks the token."""
        if not token:
            return None
        for record in self._sessions.values():
            if record.token and secrets.compare_digest(record.token, token):
                return record
        return None

    def authorize(self, session_id: str, token: str) -> Optional[SessionRecord]:
        """The session, if the token matches. A constant-time comparison, because this is the
        check that stands between one session and every other session on the machine."""
        record = self._sessions.get(session_id)
        if record is None or not record.token or not token:
            return None
        return record if secrets.compare_digest(record.token, token) else None

    def mark(self, session_id: str, *, status: str = "", updated_at: str = "", **fields) -> Optional[SessionRecord]:
        record = self._sessions.get(session_id)
        if record is None:
            return None
        if status:
            record.status = status
        if updated_at:
            record.updated_at = updated_at
        for name, value in fields.items():
            if hasattr(record, name):
                setattr(record, name, value)
        return record

    def forget(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
