"""This session's view of its peers.

The runtime offers the peer-session tools; this is what makes them work. It lives in the
worker because the worker is the layer that knows *which* session this is and holds the
connection to the daemon — the two things the tools need and the runtime deliberately does
not carry.

Everything goes through the daemon's control plane, the same surface the `frank` command and
the desktop client call. Nothing here reaches into another session's socket: a peer is driven
by asking the daemon to relay, exactly as a person would.

The one thing this adds on the caller's behalf is identity. Every create carries this
session as the parent, and it is not optional — that is what puts the child in the tree,
inside the reaper, and under the permission clamp. Passing it explicitly at each call site
would make it something a caller could forget, which is precisely how the shell-based version
went wrong.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from frank.protocol.metadata import Metadata
from frank.base.tuning import Tunable, active_tuning

logger = logging.getLogger(__name__)


class PeerSessionError(RuntimeError):
    """A control-plane call that failed, carrying the daemon's own message."""


class PeerSessions:
    """The peer-session operations available to one session."""

    def __init__(
        self,
        *,
        socket_path: str,
        token: str,
        session_id: str,
        working_directory: str,
        permission_mode: str,
        parent_session: str = "",
    ) -> None:
        self._socket_path = socket_path
        self._token = token
        self.session_id = session_id
        self.working_directory = working_directory
        self.permission_mode = permission_mode
        self._parent_session = parent_session
        # Whether this session has ever answered the one that created it.
        self.reported_to_parent = False
        self._client: Optional[httpx.AsyncClient] = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=self._socket_path),
                base_url="http://daemon",
                timeout=active_tuning().duration(Tunable.control_plane_call_seconds),
                headers={"Authorization": f"Bearer {self._token}"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _call(self, method: str, **params: Any) -> dict:
        try:
            response = await self._http().post(
                "/rpc", json={"method": method, "params": params}
            )
        except (httpx.HTTPError, OSError) as error:
            raise PeerSessionError(f"the daemon could not be reached: {error}") from error
        try:
            body = response.json()
        except ValueError as error:
            raise PeerSessionError(f"the daemon returned a malformed answer ({response.status_code})") from error
        if "error" in body:
            raise PeerSessionError(str(body["error"].get("message") or "the call failed"))
        if response.status_code >= 400:
            raise PeerSessionError(f"{method} was rejected ({response.status_code})")
        return body.get("result") or {}

    # The SessionAccess surface the runtime's tools call.

    async def create(self, *, agent: str, working_directory: str) -> dict:
        """Make a peer. It is not named here.

        A session is named after the first thing it is asked to do, and that holds whoever asks:
        a person typing into a composer, or a session sending a brief. Letting the creator pass a
        title made the same session answer to two names — the terse label its parent chose, and
        the one it generated from the brief a moment later — and which one you saw depended on
        which finished first."""
        result = await self._call(
            "session.create",
            agent=agent,
            working_directory=working_directory,
            # No mode is sent.
            parent=self.session_id,
        )
        return result.get("session") or result

    async def send(self, session_id: str, text: str) -> dict:
        """Hand another session a message, as a peer turn.

        The kind matters. Without it the message arrives with `role: "user"`, and both the
        model and the desktop client read it as the person speaking — a peer's report would be
        attributed to the user who never wrote it."""
        outcome = await self._call(
            "session.send",
            id=session_id,
            parts=[{"kind": "text", "text": text}],
            metadata={Metadata.PEER_SENDER: self.session_id},
        )
        # A refused send is not a report.
        accepted = not (isinstance(outcome, dict) and outcome.get("awaiting_input"))
        if accepted and self._parent_session and session_id == self._parent_session:
            self.reported_to_parent = True
        return outcome if isinstance(outcome, dict) else {}

    async def get(self, session_id: str) -> dict:
        """A peer's record, plus what it is waiting on when it is waiting on a person.

        `awaiting_input: true` alone says a session is blocked and not what would unblock it, so
        a caller cannot tell "parked on a permission request I should leave alone" from "never
        started". That ambiguity is what led a session to replace three peers that were working."""
        result = await self._call("session.get", id=session_id)
        return result.get("session") or {}

    async def children(self) -> list[dict]:
        """The sessions this one created, and their descendants.

        Its own subtree rather than the machine's session list: a session has no business
        enumerating work it did not start, and a listing it cannot act on is context spent for
        nothing."""
        result = await self._call("session.tree", id=self.session_id)
        return list(result.get("descendants") or [])

    async def end(self, session_id: str) -> dict:
        return await self._call("session.end", id=session_id)

    async def remote_list(self) -> list[dict]:
        result = await self._call("remote.list")
        return list(result.get("agents") or [])

    async def remote_send(self, name: str, text: str) -> dict:
        return await self._call("remote.send", name=name, text=text)
