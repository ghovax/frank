"""A session's own endpoint: the socket everything talks to once it holds the address."""

from __future__ import annotations

import logging
import secrets
from typing import Any, Awaitable, Callable

from a2a.types import DataPart, Part, TextPart

from frank.protocol.metadata import Metadata
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


logger = logging.getLogger(__name__)


def build_app(session) -> FastAPI:
    """The ASGI app a worker serves on its unix socket."""

    app = FastAPI(title=f"frank-session-{session.session_id}")

    def authorized(request: Request) -> bool:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return secrets.compare_digest(header[len("Bearer "):], session.token)

    @app.get("/.well-known/agent-card.json")
    async def agent_card() -> JSONResponse:
        """Discovery stays open: a card says what this session is, and a peer has to be able to read it before it has been handed anything."""
        return JSONResponse(session.card_payload())

    @app.post("/rpc")
    async def rpc(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"error": {"code": "unauthorized", "message": "Bad or missing token."}}, status_code=401)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": {"code": "invalid_json", "message": "Body must be JSON."}}, status_code=400)

        method = str(payload.get("method") or "")
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            return JSONResponse({"error": {"code": "invalid_request", "message": "params must be an object."}}, status_code=400)

        handler = METHODS.get(method)
        if handler is None:
            return JSONResponse({"error": {"code": "no_such_method", "message": f"Unknown method {method!r}."}}, status_code=404)
        try:
            return JSONResponse({"result": await handler(session, params)})
        except Exception as error:  # noqa: BLE001 — one bad call must not kill the session
            logger.exception("session call %s failed", method)
            return JSONResponse(
                {"error": {"code": "internal_error", "message": f"{method} failed: {error}"}},
                status_code=500,
            )

    return app


def _message_parts(params: dict) -> list[Part]:
    """Build the message's parts from either prose or an explicit part list, so a caller can send plain text without constructing the A2A shape by hand."""
    explicit = params.get("parts")
    if isinstance(explicit, list) and explicit:
        parts: list[Part] = []
        for entry in explicit:
            if not isinstance(entry, dict):
                continue
            if entry.get("kind") == "text":
                parts.append(Part(root=TextPart(text=str(entry.get("text", "")))))
            else:
                # The part's `data`, not the part.
                payload = entry.get("data") if isinstance(entry.get("data"), dict) else entry
                parts.append(Part(root=DataPart(data=dict(payload))))
        if parts:
            return parts
    return [Part(root=TextPart(text=str(params.get("text", ""))))]


async def _send(session, params: dict) -> dict:
    """Drive a turn with this message."""
    # A session parked on a human decision takes no new turn.
    parked = await session.pending_decision()
    if parked and not session.is_running:
        return {"accepted": False, "awaiting_input": True, "waiting_on": parked}
    if session.is_running:
        text = "".join(
            str(entry.get("text", ""))
            for entry in (params.get("parts") or [])
            if isinstance(entry, dict) and entry.get("kind") == "text"
        ) or str(params.get("text", ""))
        # The sender's own id for this message, carried through so the steering event can name it.
        message_id = str((params.get("metadata") or {}).get("messageId") or "")
        # Who sent it, carried into the running turn.
        peer_sender = str((params.get("metadata") or {}).get(Metadata.PEER_SENDER) or "")
        if session.inject(text, message_id, peer_sender):
            return {"accepted": True, "injected": True}
        # The turn ended between the check and the injection; fall through and start a fresh one rather than silently dropping the message.
    turn_id = await session.start_turn(_message_parts(params), dict(params.get("metadata") or {}))
    return {"accepted": True, "injected": False, "turn_id": turn_id}


async def _respond(session, params: dict) -> dict:
    """Answer a pending permission or question, unblocking the parked turn."""
    request_id = str(params.get("request_id") or "")
    if not request_id:
        raise ValueError("request_id is required")
    data: dict[str, Any] = {"request_id": request_id}
    if params.get("declined"):
        data["declined"] = True
    elif params.get("answers") is not None:
        data["answers"] = params.get("answers")
    else:
        data["decision"] = str(params.get("decision") or "deny")
    resolved = await session.resolve_pending_input(data)
    return {"resolved": resolved}


async def _cancel(session, params: dict) -> dict:
    tool_call_id = str(params.get("tool_call_id") or "")
    if tool_call_id:
        # The facade method, not the context-keyed one underneath it: a worker is one session, so the id is implicit here and passing only the tool call would be a missing argument.
        return {"cancelled": session.abort_tool_call(tool_call_id)}
    return {"cancelled": session.abort()}


async def _status(session, _params: dict) -> dict:
    return session.status_payload()


async def _abort_input(session, _params: dict) -> dict:
    return {"aborted": await session.abort_pending_input()}


async def _clear_goal(session, _params: dict) -> dict:
    """The person called the goal off."""
    return {"cleared": session.clear_goal(session.session_id)}


async def _compact(session, _params: dict) -> dict:
    return {"compacting": session.compact()}


async def _list_jobs(session, _params: dict) -> dict:
    return {"jobs": session.background_jobs()}


async def _detach_job(session, params: dict) -> dict:
    return {"backgrounded": session.background_tool_call(str(params.get("tool_call_id") or ""))}


async def _set_locations(session, params: dict) -> dict:
    """The workspace's environments were edited under a live session."""
    entries = params.get("locations")
    return {"locations": session.set_locations(entries if isinstance(entries, list) else None)}


async def _set_permission_mode(session, params: dict) -> dict:
    """The person changed this session's approval policy while it runs."""
    return {"permission_mode": await session.set_permission_mode(str(params.get("permission_mode") or ""))}


async def _reset(session, _params: dict) -> dict:
    """Settings changed under a live session."""
    session.reset_runtimes()
    return {"ok": True}


# Every verb this socket answers, in one table.
METHODS: dict[str, Callable[[Any, dict], Awaitable[dict]]] = {
    "message/send": _send,
    "input/respond": _respond,
    "input/abort": _abort_input,
    "tasks/cancel": _cancel,
    "session/status": _status,
    "session/goal-clear": _clear_goal,
    "session/compact": _compact,
    "session/locations": _set_locations,
    "session/permission-mode": _set_permission_mode,
    "session/reset": _reset,
    "jobs/list": _list_jobs,
    "jobs/detach": _detach_job,
}
