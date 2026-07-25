"""The daemon's control plane: what clients call to make sessions exist and to read them.

One method surface, reached two ways. The CLI and sessions connect over a unix socket in the
runtime directory; the desktop client connects over a loopback TCP port, because a webview
cannot open a unix socket. Both carry a capability token, so the API is closed to anything
that cannot read the 0600 token file — which is what finally puts authentication in front of
a surface that executes tools.

Two kinds of token, and the difference matters. The daemon's says a caller may drive the
daemon and nothing about who it is; a session's own says *which* session is calling. A caller
identified that way is held to what a session may legitimately do — its own verbs, aimed at
its own subtree — and its calls are attributed to it, which is what makes a peer it creates a
child of it rather than whatever the request body claimed.

Reads and lifecycle are served from here, because the daemon is the sole writer and therefore
already holds everything, whether a session is alive or long since reaped. Commands are a
different matter: they belong to the session that runs them, so `session.send` and its
siblings are relayed to that session's own socket rather than answered here. The daemon stays
out of the path between two peers; it only carries what a human's client cannot address.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from xeac.base.permission_mode import PermissionMode
from xeac.daemon import state
from xeac.daemon.registry import RUNNING, SessionRecord
from xeac.base.serialization import compact

logger = logging.getLogger(__name__)

router = APIRouter()


class RpcError(Exception):
    """A control-plane call that cannot be served, with the status a client should see."""

    def __init__(self, message: str, status_code: int = 400, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require(params: dict, name: str) -> str:
    value = str(params.get(name) or "").strip()
    if not value:
        raise RpcError(f"{name} is required")
    return value


def _session(session_id: str) -> SessionRecord:
    record = state.registry.get(session_id) if state.registry else None
    if record is None:
        raise RpcError(f"No session {session_id!r}.", status_code=404, code="no_such_session")
    return record


def _assert_session_known(session_id: str) -> None:
    """Refuse an id nothing has ever heard of.

    Only worth asking when a read came back empty, and only then because empty and unknown
    look identical to a caller: a mistyped id would otherwise read as a session that simply
    has not said anything yet. The registry alone is not the test — it holds what *this*
    daemon started, while the store outlives restarts — so a session is known if either
    remembers it, and the store has already been consulted by the time this is called."""
    if state.registry is not None and state.registry.get(session_id) is not None:
        return
    raise RpcError(f"No session {session_id!r}.", status_code=404, code="no_such_session")


def _assert_agent_exists(agent: str, working_directory: str) -> None:
    """Refuse a session for an agent profile that is not there.

    Without this a mistyped `--agent` mints a session that reports itself running and only
    fails when it is first messaged, by which point the cause is several steps behind."""
    from xeac.base.configuration import list_agents

    configuration = state.global_configuration
    if configuration is None:
        return
    directories = (
        configuration.agent_directories_for(working_directory)
        if working_directory
        else configuration.agent_directories()
    )
    available = [entry["id"] for entry in list_agents(directories)]
    if agent not in available:
        known = ", ".join(sorted(available)) or "none found"
        raise RpcError(
            f"No agent profile named {agent!r}. Available: {known}.",
            status_code=404,
            code="no_such_agent",
        )


def _public(record: SessionRecord) -> dict:
    """A session as a client sees it: what the registry knows plus what it is doing.

    The registry tracks the *process* — a session is "running" from the moment its worker
    binds until it is reaped, including the long stretches where it sits idle between
    messages. Whether a turn is actually in flight is something only the session can report,
    and it does, over ingest. Merging the two here is what lets a client tell a session that
    is working from one that is merely alive; keeping them separate fields is what stops a
    listing from claiming a reaped session is busy."""
    payload = record.public()
    payload["busy"] = record.id in state._running_contexts
    return payload


async def _session_create(params: dict) -> dict:
    """Mint a session and hand back its handle.

    This is the only place a session's configuration is set. The mode is clamped against the
    parent's, so a child can never be created looser than the session that created it — the
    clamp lives here rather than in the caller because the caller is often the model."""
    assert state.registry is not None and state.lifecycle is not None
    # No fallback: which agent a session runs is the one thing nothing can reasonably guess
    # on the caller's behalf. A default here would mean a mistyped or forgotten `--agent`
    # silently produced a session doing work under a profile nobody chose.
    agent = _require(params, "agent")
    _assert_agent_exists(agent, str(params.get("working_directory") or ""))
    # A session that authenticated as itself is the parent, whatever it asked for. The clamp
    # and the reaper both hang off this link, so leaving it to the caller to declare made both
    # opt-out: a session could create a peer outside its own tree, at any mode, simply by not
    # mentioning itself. An unattributed call (a person's client) still passes `parent`.
    parent_id = str(params.get("calling_session") or params.get("parent") or "").strip()
    parent = state.registry.get(parent_id) if parent_id else None
    if parent_id and parent is None:
        raise RpcError(f"No parent session {parent_id!r}.", status_code=404, code="no_such_session")

    # Precedence: what the caller asked for, else the configured default. Either way a child
    # is clamped against its parent, so the fallback can never be a way to gain authority the
    # parent did not have.
    configured = getattr(getattr(state.global_configuration, "agent", None), "permission_mode", "")
    requested = str(params.get("permission_mode") or "") or str(configured or "")
    mode = (
        PermissionMode.more_restrictive(requested, parent.permission_mode)
        if parent is not None
        else PermissionMode.coerce(requested)
    )

    working_directory = str(params.get("working_directory") or "")
    if parent is not None and not working_directory:
        working_directory = parent.working_directory

    record = state.registry.create(
        agent=agent,
        working_directory=working_directory,
        permission_mode=str(mode),
        project_id=str(params.get("project_id") or (parent.project_id if parent else "")),
        parent=parent_id,
        title=str(params.get("title") or ""),
        created_at=_now(),
    )

    # Where the session will actually run, and its durable row, decided here rather than on
    # its first turn: the workspace strategy can put a session in its own git worktree, and a
    # session whose tools do not yet know which directory they operate on is not a session
    # anyone can safely message. It is also the row the title and the draft later land on.
    from xeac.daemon.services.sessions import _ensure_session_workspace

    try:
        workspace = await asyncio.to_thread(
            _ensure_session_workspace,
            record.id,
            record.agent,
            record.working_directory,
            str(params.get("workspace_strategy") or ""),
            record.permission_mode,
            record.project_id,
        )
        state.registry.mark(record.id, runtime_working_directory=workspace.runtime_working_directory)
    except Exception:  # noqa: BLE001 — a workspace that cannot be prepared is not a fatal
        logger.exception("Could not prepare a workspace for session %s", record.id)
        state.registry.mark(record.id, runtime_working_directory=record.working_directory)

    started = await state.lifecycle.start(record)
    if not started:
        raise RpcError(
            f"Session {record.id} could not be started ({record.exit_reason or 'unknown reason'}).",
            status_code=503,
            code="worker_unavailable",
        )
    # The token is returned exactly once, here, to whoever asked for the session. The parent
    # and mode come back too because both may differ from what was asked for — a caller
    # attributed by its token becomes the parent whatever it said, and the mode is clamped
    # against that parent — and a creator that cannot see the difference cannot reason about
    # what it just made.
    return {
        "id": record.id,
        "token": record.token,
        "socket": str(record.socket_path),
        "agent": record.agent,
        "parent": record.parent,
        "permission_mode": record.permission_mode,
    }


async def _session_list(params: dict) -> dict:
    assert state.registry is not None
    include_terminal = bool(params.get("all"))
    records = state.registry.all() if include_terminal else state.registry.live()
    parent = str(params.get("parent") or "")
    if parent:
        records = [record for record in records if record.parent == parent]
    return {"sessions": [_public(record) for record in sorted(records, key=lambda entry: entry.created_at)]}


async def _session_get(params: dict) -> dict:
    return {"session": _public(_session(_require(params, "id")))}


async def _session_tree(params: dict) -> dict:
    """A session and everything under it, so a client can render the hierarchy that creating a
    peer builds up. Without this a fan-out just looks like a pile of unrelated sessions."""
    assert state.registry is not None
    root = _session(_require(params, "id"))
    return {
        "session": _public(root),
        "descendants": [_public(record) for record in state.registry.descendants_of(root.id)],
    }


async def _session_end(params: dict) -> dict:
    assert state.lifecycle is not None
    record = _session(_require(params, "id"))
    reaped = await state.lifecycle.reap(record.id, reason=str(params.get("reason") or "killed by request"))
    return {"killed": record.id, "reaped": reaped}


async def _session_send(params: dict) -> dict:
    """Relay a message to the session's own socket.

    A message to a session that is mid-turn is injected at its next safe point rather than
    queued behind the whole turn, which is what makes a peer's question reach a working
    session instead of waiting for it to finish."""
    record = _session(_require(params, "id"))
    if record.status != RUNNING:
        raise RpcError(
            f"Session {record.id} is {record.status}, so it cannot accept messages.",
            status_code=409,
            code="session_not_running",
        )
    return await state.relay_to_session(record, "message/send", params)


async def _turn_cancel(params: dict) -> dict:
    record = _session(_require(params, "id"))
    return await state.relay_to_session(record, "tasks/cancel", params)


async def _session_respond(params: dict) -> dict:
    """Answer a session's pending human-in-the-loop gate."""
    record = _session(_require(params, "id"))
    _require(params, "request_id")
    return await state.relay_to_session(record, "input/respond", params)


async def _session_compact(params: dict) -> dict:
    """Ask a session to compact its own conversation."""
    record = _session(_require(params, "id"))
    return await state.relay_to_session(record, "session/compact", params)


async def _jobs_list(params: dict) -> dict:
    """What background work a session has in flight. Read from the session rather than the
    store: a background job lives in the process running it."""
    record = _session(_require(params, "id"))
    return await state.relay_to_session(record, "jobs/list", params)


async def _jobs_detach(params: dict) -> dict:
    """Detach a still-blocking command so the session's turn can continue without it."""
    record = _session(_require(params, "id"))
    _require(params, "tool_call_id")
    return await state.relay_to_session(record, "jobs/detach", params)


async def _session_history(params: dict) -> dict:
    """A session's turns, read from the store rather than the session.

    Served here on purpose: the daemon is the sole writer, so history is readable whether the
    session is running, parked, or was reaped an hour ago.

    With a limit this pages backwards through the store, returning the cursor for the next page
    — which is what lets a client show a long session immediately and pull the rest behind it,
    rather than waiting on every turn it has ever had."""
    assert state.turn_store is not None
    session_id = _require(params, "id")
    limit = int(params.get("limit") or 0)
    if limit <= 0:
        turns = await state.turn_store.turns_for_session(session_id)
        if not turns:
            _assert_session_known(session_id)
        return {
            "turns": [turn.model_dump(by_alias=True, exclude_none=True, mode="json") for turn in turns],
            "next_before_row_id": None,
            "has_more": False,
        }
    raw_cursor = params.get("before_row_id")
    page = await state.turn_store.turn_page_for_session(
        session_id,
        limit=limit,
        before_row_id=int(raw_cursor) if raw_cursor is not None else None,
    )
    turns = page.get("turns") or []
    if not turns and raw_cursor is None:
        _assert_session_known(session_id)
    return {
        "turns": [
            turn.model_dump(by_alias=True, exclude_none=True, mode="json")
            if hasattr(turn, "model_dump") else turn
            for turn in turns
        ],
        "next_before_row_id": page.get("next_before_row_id"),
        "has_more": bool(page.get("has_more")),
    }


async def _turn_get(params: dict) -> dict:
    assert state.turn_store is not None
    turn = await state.turn_store.get(_require(params, "turn_id"))
    if turn is None:
        raise RpcError("No such turn.", status_code=404, code="no_such_turn")
    return {"turn": turn.model_dump(by_alias=True, exclude_none=True, mode="json", exclude={"history"})}


async def _remote_list(_params: dict) -> dict:
    """The peers registered on other hosts, with their live health.

    Listed apart from sessions because they are a different kind of thing: XEAC does not own
    their lifecycle, cannot set their permission mode, and keeps no transcript of them. What
    it has is an address and a card."""
    assert state.global_configuration is not None
    manager = state.remote_agent_manager
    agents = []
    for name, configuration in state.global_configuration.remote_agents.agents.items():
        health = manager.health(name) if manager is not None else {"health": "unconfigured", "error": ""}
        card = manager.card(name) if manager is not None else None
        agents.append({
            "name": name,
            "card_url": configuration.card_url,
            "enabled": configuration.enabled,
            "health": health["health"],
            "error": health["error"],
            "description": (card.description if card is not None else "") or "",
        })
    return {"agents": sorted(agents, key=lambda entry: entry["name"])}


async def _remote_send(params: dict) -> dict:
    """Hand one message to a registered remote peer and return what it produced.

    One-shot on purpose: a remote agent runs on someone else's machine, at their cost, with no
    shared history and no access to this filesystem. That is a different bargain from a local
    peer, so it gets a different verb rather than being smuggled into `session.send` — a caller
    should never be unsure which side of the wire its work went to."""
    from a2a.types import Message, Part, Role, TextPart

    name = _require(params, "name")
    text = str(params.get("text") or "")
    manager = state.remote_agent_manager
    if manager is None:
        raise RpcError("No remote agents are configured.", status_code=404, code="no_remote_agents")
    message = Message(
        role=Role.user,
        parts=[Part(root=TextPart(text=text))],
        message_id=uuid.uuid4().hex,
    )
    collected: list[str] = []
    try:
        async for event in manager.send_message(name, message):
            for part in _remote_text_parts(event):
                collected.append(part)
    except LookupError as error:
        raise RpcError(str(error), status_code=404, code="no_such_remote_agent") from error
    except Exception as error:  # noqa: BLE001 — an unreachable peer is an answer, not a crash
        raise RpcError(f"{name} could not be reached: {error}", status_code=502, code="remote_unreachable") from error
    return {"name": name, "text": "".join(collected)}


def _remote_text_parts(event: Any) -> list[str]:
    """The prose in one streamed A2A event, whatever shape it arrived in.

    A remote agent may answer with a bare Message, or with a Task whose artifacts carry the
    result; both are normal, so both are read rather than assuming the shape a particular peer
    happens to use."""
    texts: list[str] = []
    candidates = event if isinstance(event, tuple) else (event,)
    for candidate in candidates:
        for part in getattr(candidate, "parts", None) or []:
            text = getattr(getattr(part, "root", part), "text", "")
            if text:
                texts.append(str(text))
        for artifact in getattr(candidate, "artifacts", None) or []:
            for part in getattr(artifact, "parts", None) or []:
                text = getattr(getattr(part, "root", part), "text", "")
                if text:
                    texts.append(str(text))
    return texts


async def _daemon_status(_params: dict) -> dict:
    assert state.registry is not None
    live = state.registry.live()
    return {
        "ok": True,
        "sessions": {"live": len(live), "total": len(state.registry.all())},
        "pool": {
            "warm": state.pool.warm_count if state.pool else 0,
            "assigned": state.pool.assigned_count if state.pool else 0,
        },
        "socket": str(state.daemon_socket),
        "port": state.daemon_port,
    }


METHODS: dict[str, Callable[[dict], Awaitable[dict]]] = {
    "session.create": _session_create,
    "session.list": _session_list,
    "session.get": _session_get,
    "session.tree": _session_tree,
    "session.end": _session_end,
    "session.send": _session_send,
    "turn.cancel": _turn_cancel,
    "session.respond": _session_respond,
    "session.compact": _session_compact,
    "jobs.list": _jobs_list,
    "jobs.detach": _jobs_detach,
    "session.history": _session_history,
    "remote.list": _remote_list,
    "remote.send": _remote_send,
    "turn.get": _turn_get,
    "daemon.status": _daemon_status,
}


# What a session may ask the control plane for on its own behalf. Narrower than what a
# person's client may do, and deliberately so: a session token is a capability for one
# session's work, not a second daemon token. Everything absent here — reading another tree's
# history, answering a permission request, compacting somebody else — stays with the human.
_SESSION_CALLER_METHODS = frozenset({
    "session.create", "session.send", "session.get", "session.tree",
    "session.end", "session.history", "remote.list", "remote.send",
})


def _refuse_session_caller(caller: str, method: str, params: dict) -> Optional[RpcError]:
    """Whether an attributed session may make this call, and why not.

    Two limits. A session may only use the verbs it composes with, and it may only aim them at
    itself or something it created — its own subtree. Without the second, a session token
    would be a handle on every other session on the machine, which is the opposite of what
    minting one per session is for.

    One exception, and it is the return path: a session may `session.send` to its own parent.
    A peer that cannot answer the session that created it is not a peer, it is a fire-and-
    forget job, and the alternative — the caller reconstructing an answer out of the peer's
    durable record — is what this exception exists to have deleted. It is deliberately the
    narrowest widening that makes a reply possible: one verb, one recipient, and nothing else
    moves upward. A session still cannot read its parent's history or end it."""
    if method not in _SESSION_CALLER_METHODS:
        return RpcError(f"A session may not call {method!r}.", status_code=403, code="forbidden")
    target = str(params.get("id") or "").strip()
    if not target or target == caller:
        return None
    if state.registry is None:
        return None
    if any(record.id == target for record in state.registry.descendants_of(caller)):
        return None
    if method == "session.send":
        own = state.registry.get(caller)
        if own is not None and own.parent and own.parent == target:
            return None
    return RpcError(
        f"Session {target!r} is not yours.", status_code=403, code="forbidden",
    )


@router.post("/rpc")
async def rpc(request: Request) -> JSONResponse:
    """One entry point for every control-plane call."""
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
    # Who is calling, when the token said so. A session's own token identifies it; the daemon
    # token does not, and leaves this empty. Overwriting rather than merging is the point: a
    # caller must not be able to name itself something other than what its token proves.
    caller = getattr(request.state, "calling_session", "")
    if caller:
        refusal = _refuse_session_caller(caller, method, params)
        if refusal is not None:
            return JSONResponse({"error": {"code": refusal.code, "message": refusal.message}}, status_code=refusal.status_code)
        params = {**params, "calling_session": caller}
    try:
        return JSONResponse({"result": await handler(params)})
    except RpcError as error:
        return JSONResponse({"error": {"code": error.code, "message": error.message}}, status_code=error.status_code)
    except Exception as error:  # noqa: BLE001 — one bad call must not take the daemon down
        logger.exception("Control-plane call %s failed", method)
        return JSONResponse(
            {"error": {"code": "internal_error", "message": f"{method} failed: {error}"}},
            status_code=500,
        )


@router.get("/sessions/{session_id}/attach")
async def attach(session_id: str, request: Request) -> EventSourceResponse:
    """Watch a session: a snapshot of what has happened, then everything as it happens.

    The snapshot comes first so a client that attaches mid-turn is not left guessing about
    what it missed, and the live tail continues from there."""
    _session(session_id)
    # Same scoping as the control plane: a session's own token watches its own subtree, not
    # every stream on the machine. A human's client presents the daemon token and is not
    # narrowed — watching is what it exists to do.
    caller = getattr(request.state, "calling_session", "")
    if caller:
        refusal = _refuse_session_caller(caller, "session.get", {"id": session_id})
        if refusal is not None:
            raise refusal

    async def stream():
        assert state.turn_store is not None
        subscription = state.event_bus.subscribe(session_id)
        try:
            turns = await state.turn_store.turns_for_session(session_id)
            yield {
                "data": compact({
                    "kind": "snapshot",
                    "turns": [turn.model_dump(by_alias=True, exclude_none=True, mode="json") for turn in turns],
                })
            }
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(subscription.get(), timeout=15)
                except asyncio.TimeoutError:
                    # A comment keeps the connection warm through proxies without inventing an
                    # event the client would have to ignore.
                    yield {"comment": "keepalive"}
                    continue
                if event is None:
                    yield {"data": compact({"kind": "done"})}
                    break
                if "turn" in event:
                    # A turn started or ended. Distinct from `done`, which is the session
                    # itself ending: a session goes idle many times over its life, and a
                    # watcher that conflated the two would either stop after the first turn
                    # or wait for a process to die.
                    yield {"data": compact({
                        "kind": "turn",
                        "seq": event.get("seq", 0),
                        "running": bool((event.get("turn") or {}).get("running")),
                    })}
                    continue
                yield {"data": compact({"kind": "live", "seq": event.get("seq", 0), "message": event.get("part")})}
        finally:
            state.event_bus.unsubscribe(session_id, subscription)

    return EventSourceResponse(stream())


@router.get("/events")
async def events(request: Request) -> EventSourceResponse:
    """The daemon-wide bus: sessions appearing and ending, configuration changing."""

    async def stream():
        subscription = state.broadcaster.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(subscription.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"comment": "keepalive"}
                    continue
                if event is None:
                    # The daemon is going down and has closed the bus. Ending here is what lets
                    # it finish: a server draining its connections cannot outwait a stream that
                    # is waiting on the server.
                    break
                yield {"data": compact(event)}
        finally:
            state.broadcaster.unsubscribe(subscription)

    return EventSourceResponse(stream())
