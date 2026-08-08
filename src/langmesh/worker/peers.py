"""This session's view of its peers, in the worker because that is what knows which session this is."""

from __future__ import annotations

import logging
from typing import Any

from langmesh.protocol.metadata import Metadata

logger = logging.getLogger(__name__)


class PeerSessionError(RuntimeError):
    """A control-plane call that failed, carrying the daemon's own message."""


class PeerSessions:
    """The peer-session operations available to one session."""

    def __init__(
        self,
        *,
        session_id: str,
        working_directory: str,
        permission_mode: str,
        parent_session: str = "",
    ) -> None:
        self.session_id = session_id
        self.working_directory = working_directory
        self.permission_mode = permission_mode
        self._parent_session = parent_session
        # Whether this session has answered its creator, tracked here because this is the only way out.
        self.reported_to_parent = False

    async def aclose(self) -> None:
        """Nothing is held open: the control plane is the daemon this session runs inside."""
        return None

    async def _call(self, method: str, **params: Any) -> dict:
        """Run one control-plane verb, scoped to this session exactly as the socket would have scoped it."""
        from langmesh.daemon.api import METHODS, _refuse_session_caller, RpcError

        handler = METHODS.get(method)
        if handler is None:
            raise PeerSessionError(f"the daemon has no verb {method!r}")
        # The same refusal the socket applies, kept here so a session's reach does not depend on its transport.
        refusal = _refuse_session_caller(self.session_id, method, params)
        if refusal is not None:
            raise PeerSessionError(refusal.message)
        try:
            result = await handler({**params, "calling_session": self.session_id})
        except RpcError as error:
            raise PeerSessionError(error.message) from error
        except Exception as error:  # noqa: BLE001 — surfaced to the model as a failed tool result
            raise PeerSessionError(str(error)) from error
        return result or {}

    # The SessionAccess surface the runtime's tools call.

    async def create(
        self,
        *,
        agent: str,
        working_directory: str,
        inherited_conversation: list[dict[str, Any]],
    ) -> dict:
        """Make a peer that inherits this session's conversation through a shared snapshot. It is not named here: a session is named after the first thing it is asked to do."""
        result = await self._call(
            "session.create",
            agent=agent,
            working_directory=working_directory,
            inherited_conversation=inherited_conversation,
            # No mode is sent: the daemon gives a child its parent's, narrowed by the profile's ceiling.
            parent=self.session_id,
        )
        return result

    async def send(self, session_id: str, text: str) -> dict:
        """Hand another session a message as a peer turn, or the model reads a report as the person speaking."""
        outcome = await self._call(
            "session.send",
            id=session_id,
            parts=[{"kind": "text", "text": text}],
            metadata={Metadata.PEER_SENDER: self.session_id},
        )
        # A refused send is not a report, so it must not silence the reminder that exists for that case.
        accepted = not (isinstance(outcome, dict) and outcome.get("awaiting_input"))
        if accepted and self._parent_session and session_id == self._parent_session:
            self.reported_to_parent = True
        return outcome if isinstance(outcome, dict) else {}

    async def get(self, session_id: str) -> dict:
        """A peer's record, plus what it is waiting on, since "blocked" alone cannot be acted on."""
        result = await self._call("session.get", id=session_id)
        return result["session"]

    async def children(self) -> list[dict]:
        """The sessions this one created, and their descendants: its own subtree, not the machine's."""
        result = await self._call("session.tree", id=self.session_id)
        return list(result.get("descendants") or [])

    async def end(self, session_id: str) -> dict:
        return await self._call("session.end", id=session_id)

    async def remote_list(self) -> list[dict]:
        result = await self._call("remote.list")
        return list(result.get("agents") or [])

    async def remote_send(self, name: str, text: str) -> dict:
        return await self._call("remote.send", name=name, text=text)
