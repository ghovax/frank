"""A session's own endpoint: the socket everything talks to once it holds the address.

The surface is small on purpose — send a message, answer a gate, cancel a turn, read the
card. Anything that is not driving *this* session is the daemon's business, not a session's.

Every request must carry the session's capability token. The socket file is already
restricted to the user, so this guards the boundary that matters here: one session reaching
another it was never handed. A peer that was given an address and a token can drive it; a
process that merely guessed an id cannot.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from a2a.types import DataPart, Part, TextPart
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


logger = logging.getLogger(__name__)


def build_app(session) -> FastAPI:
    """The ASGI app a worker serves on its unix socket. ``session`` is the live
    :class:`SessionExecutor` this process is."""

    app = FastAPI(title=f"daisy-session-{session.session_id}")

    def authorized(request: Request) -> bool:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return secrets.compare_digest(header[len("Bearer "):], session.token)

    @app.get("/.well-known/agent-card.json")
    async def agent_card() -> JSONResponse:
        """Discovery stays open: a card says what this session is, and a peer has to be able
        to read it before it has been handed anything. It carries no conversation content.

        This is the only well-known card Daisy serves. The daemon does not have one, because
        the daemon is not an agent — answering there would mean electing some profile to
        speak for it, which is a default agent by another name."""
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

        try:
            if method == "message/send":
                return JSONResponse({"result": await _send(session, params)})
            if method == "input/respond":
                return JSONResponse({"result": await _respond(session, params)})
            if method == "tasks/cancel":
                return JSONResponse({"result": await _cancel(session, params)})
            if method == "session/status":
                return JSONResponse({"result": session.status_payload()})
            if method == "input/abort":
                return JSONResponse({"result": {"aborted": await session.abort_pending_input()}})
            if method == "session/compact":
                return JSONResponse({"result": {"compacting": session.compact()}})
            if method == "jobs/list":
                return JSONResponse({"result": {"jobs": session.background_jobs()}})
            if method == "jobs/detach":
                identifier = str(params.get("tool_call_id") or "")
                return JSONResponse({"result": {"backgrounded": session.background_tool_call(identifier)}})
            if method == "session/reset":
                # Settings changed under a live session. Drop the cached runtime so the next
                # turn rebuilds it against the new configuration, rather than the session
                # keeping the model and tool set it happened to start with.
                session.reset_runtimes()
                return JSONResponse({"result": {"ok": True}})
        except Exception as error:  # noqa: BLE001 — one bad call must not kill the session
            logger.exception("Session call %s failed", method)
            return JSONResponse(
                {"error": {"code": "internal_error", "message": f"{method} failed: {error}"}},
                status_code=500,
            )
        return JSONResponse({"error": {"code": "no_such_method", "message": f"Unknown method {method!r}."}}, status_code=404)

    return app


def _message_parts(params: dict) -> list[Part]:
    """Build the message's parts from either prose or an explicit part list, so a caller can
    send plain text without constructing the A2A shape by hand."""
    explicit = params.get("parts")
    if isinstance(explicit, list) and explicit:
        parts: list[Part] = []
        for entry in explicit:
            if not isinstance(entry, dict):
                continue
            if entry.get("kind") == "text":
                parts.append(Part(root=TextPart(text=str(entry.get("text", "")))))
            else:
                parts.append(Part(root=DataPart(data=dict(entry))))
        if parts:
            return parts
    return [Part(root=TextPart(text=str(params.get("text", ""))))]


async def _send(session, params: dict) -> dict:
    """Drive a turn with this message.

    A message that arrives while the session is mid-turn is injected at the turn's next safe
    point rather than starting a second one — that is what makes a peer's question reach a
    working session instead of waiting for it to go idle."""
    if session.is_running:
        text = "".join(
            str(entry.get("text", ""))
            for entry in (params.get("parts") or [])
            if isinstance(entry, dict) and entry.get("kind") == "text"
        ) or str(params.get("text", ""))
        if session.inject(text):
            return {"accepted": True, "injected": True}
        # The turn ended between the check and the injection; fall through and start a fresh
        # one rather than silently dropping the message.
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
        # The facade method, not the context-keyed one underneath it: a worker is one session,
        # so the id is implicit here and passing only the tool call would be a missing argument.
        return {"cancelled": session.abort_tool_call(tool_call_id)}
    return {"cancelled": session.abort()}
