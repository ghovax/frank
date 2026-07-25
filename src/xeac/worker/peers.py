"""This session's view of its peers.

The runtime offers the peer-session tools; this is what makes them work. It lives in the
worker because the worker is the layer that knows *which* session this is and holds the
connection to the daemon — the two things the tools need and the runtime deliberately does
not carry.

Everything goes through the daemon's control plane, the same surface the `xeac` command and
the desktop client call. Nothing here reaches into another session's socket: a peer is driven
by asking the daemon to relay, exactly as a person would.

The one thing this adds on the caller's behalf is identity. Every create carries this
session as the parent, and it is not optional — that is what puts the child in the tree,
inside the reaper, and under the permission clamp. Passing it explicitly at each call site
would make it something a caller could forget, which is precisely how the shell-based version
went wrong.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# A peer's turn can be long. There is no deadline on waiting for one — the wait is a
# subscription to the daemon's stream, and the stream is idle while the peer thinks, so a
# read timeout here would abort a turn that is simply taking its time.
_CALL_TIMEOUT_SECONDS = 60.0


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
    ) -> None:
        self._socket_path = socket_path
        self._token = token
        self.session_id = session_id
        self.working_directory = working_directory
        self.permission_mode = permission_mode
        self._client: Optional[httpx.AsyncClient] = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=self._socket_path),
                base_url="http://daemon",
                timeout=_CALL_TIMEOUT_SECONDS,
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

    # -- the SessionAccess surface the runtime's tools call ------------------------------

    async def create(self, *, agent: str, working_directory: str, permission_mode: str, title: str) -> dict:
        result = await self._call(
            "session.create",
            agent=agent,
            working_directory=working_directory,
            permission_mode=permission_mode,
            title=title,
            # Not a parameter of the tool. The caller is the parent, always.
            parent=self.session_id,
        )
        return result.get("session") or result

    async def send(self, session_id: str, text: str) -> str:
        """Hand a peer a message and return the task id it was accepted as.

        The id is what makes the wait that follows race-free: without it, a turn that finished
        between the send and the subscription is indistinguishable from one that never
        started."""
        result = await self._call(
            "session.send", id=session_id, parts=[{"kind": "text", "text": text}]
        )
        return str(result.get("task_id") or "")

    async def get(self, session_id: str) -> dict:
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
        return await self._call("session.kill", id=session_id)

    async def remote_list(self) -> list[dict]:
        result = await self._call("remote.list")
        return list(result.get("agents") or [])

    async def remote_send(self, name: str, text: str) -> dict:
        return await self._call("remote.send", name=name, text=text)

    async def await_result(self, session_id: str, task_id: str = "") -> dict:
        """Block until a peer's turn ends, then return what it produced.

        A subscription, not a poll. The daemon publishes a turn's start and end on the
        session's stream; this returns on the ending edge and then reads the turn from the
        store. The snapshot the stream opens with is sent after the subscription exists, so a
        turn that finished in the meantime is visible there rather than waited for forever."""
        await self._await_idle(session_id, task_id)
        result = await self._call("session.history", id=session_id, limit=1)
        tasks = result.get("tasks") or []
        if not tasks:
            return {"state": "unknown", "text": ""}
        task = tasks[-1]
        return {
            "state": str(((task.get("status") or {}).get("state")) or ""),
            "text": _deliverable(task),
        }

    async def _await_idle(self, session_id: str, task_id: str = "") -> None:
        headers = {"Authorization": f"Bearer {self._token}"}
        transport = httpx.AsyncHTTPTransport(uds=self._socket_path)
        # No timeout on the stream itself: it is quiet while the peer works, and a read
        # deadline would end the wait rather than the turn.
        async with httpx.AsyncClient(transport=transport, timeout=None, headers=headers) as client:
            async with client.stream("GET", f"http://daemon/sessions/{session_id}/attach") as response:
                if response.status_code >= 400:
                    raise PeerSessionError(f"could not watch {session_id} ({response.status_code})")
                buffer = ""
                async for chunk in response.aiter_text():
                    buffer += chunk.replace("\r\n", "\n")
                    while "\n\n" in buffer:
                        frame, buffer = buffer.split("\n\n", 1)
                        for line in frame.splitlines():
                            if not line.startswith("data:"):
                                continue
                            try:
                                event = json.loads(line[5:].strip())
                            except ValueError:
                                continue
                            if event.get("kind") == "snapshot":
                                # The turn may already be over. The snapshot is sent after the
                                # subscription exists, so anything ending from here on still
                                # reaches us — which makes reading it here a check rather than
                                # a guess. Without this a peer that answered inside the
                                # round-trip was waited on until it died.
                                if _finished(event.get("tasks") or [], task_id):
                                    return
                            elif _is_turn_end(event) or event.get("kind") == "done":
                                return


# A task the peer is still driving. Anything else — completed, failed, canceled, or parked on
# a human — will not move again on its own, which is when a waiter should be handed its answer.
_IN_FLIGHT = frozenset({"submitted", "working"})


def _is_turn_end(event: dict) -> bool:
    return event.get("kind") == "turn" and not event.get("running")


def _finished(tasks: list, task_id: str) -> bool:
    """Whether the turn we are waiting on is already over, judged from a snapshot."""
    if task_id and not any(task.get("id") == task_id for task in tasks):
        # Ours has not been persisted yet. It is in flight by definition — the send was
        # accepted — so its absence must not read as an idle peer.
        return False
    return not any(
        str((task.get("status") or {}).get("state") or "") in _IN_FLIGHT for task in tasks
    )


def _deliverable(task: dict) -> str:
    """What a peer produced, as prose.

    The result artifact first: that is the turn's deliverable, written once when the turn
    closes. Its final status message is the fallback, because a turn that failed or parked has
    no artifact but still has something worth saying — and returning nothing at all would read
    as a peer that did the work and declined to report it."""
    for artifact in task.get("artifacts") or []:
        if artifact.get("name") == "result":
            text = _text_of(artifact.get("parts") or [])
            if text:
                return text
    message = (task.get("status") or {}).get("message") or {}
    return _text_of(message.get("parts") or [])


def _text_of(parts: list) -> str:
    collected: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("kind") == "text" and part.get("text"):
            collected.append(str(part["text"]))
        data = part.get("data")
        if isinstance(data, dict) and data.get("message"):
            # A structured event (a failure, a permission stop) whose message is the only
            # human-readable thing the turn left behind.
            collected.append(str(data["message"]))
    return "\n".join(collected).strip()
