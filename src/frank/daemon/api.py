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
import contextlib
import dataclasses
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from frank.base.permission_mode import PermissionMode
from frank.base import telemetry
from frank.daemon import state
from frank.daemon.registry import SessionRecord
from frank.protocol.turn_record import TurnRecord
from frank.base.serialization import compact

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
    from frank.base.configuration import list_agents

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
    """A session as a client sees it.

    `busy` is set from the sessions actually mid-turn — only a session can know that, and it
    reports it over ingest — before `activity` is derived, so a listing distinguishes working
    from merely alive without any of it being written down. The record itself carries only
    what is durable: whether the session exists, not what it is doing."""
    record.busy = record.id in state._running_contexts
    return {**record.public(), "goal": state._session_goals.get(record.id)}


def _resolve_sandbox(agent: str, working_directory: str, parent, read_only: bool = False) -> dict:
    """The confinement a new session gets: the machine's, narrowed by the agent's, clamped by its
    creator's.

    Clamped rather than merely chosen, and for the same reason the permission mode is: without it
    a confined session could create an unconfined peer and the boundary would be one call deep.
    The refusal when no backend can enforce the profile happens here, at creation, because that is
    the last moment it can be reported to whoever asked for the session rather than surfacing later
    as a tool that mysteriously fails."""
    from frank.base import confinement
    from frank.hub.services.agents import _agent_configuration_for_request

    configured = getattr(state.global_configuration, "sandbox", None)
    profile = configured.to_profile() if configured is not None else confinement.Profile()
    try:
        _, agent_configuration = _agent_configuration_for_request(agent, working_directory)
        agent_profile = getattr(agent_configuration, "sandbox", None)
    except Exception:  # noqa: BLE001 — an unreadable profile must not decide confinement
        agent_profile = None
    if agent_profile is not None:
        profile = agent_profile.to_profile().clamp(profile)
    if parent is not None:
        profile = profile.clamp(confinement.Profile.from_dict(parent.sandbox))
    # A session asked for read-only is read-only at the kernel: a profile with nowhere writable.
    # Nothing about a command's text decides it, so there is no spelling of a write that gets
    # past — the operating system refuses it, and a grant cannot widen what was never offered.
    if read_only:
        profile = dataclasses.replace(
            profile,
            filesystem=dataclasses.replace(profile.filesystem, writable=(), grantable=()),
            network=False,
        )
    if profile.enforce == confinement.ENFORCE_REQUIRED and not confinement.backend_name():
        raise RpcError(
            f"Confinement is required and this machine has no backend for it ({confinement.describe_backend()}). "
            "Set sandbox.enforce to 'preferred' to run with resource limits only, or 'off' to disable it.",
            status_code=503,
            code="confinement_unavailable",
        )
    return profile.as_dict()


def _agent_permission_ceiling(agent: str, working_directory: str) -> Optional[PermissionMode]:
    """The loosest mode the agent's own profile allows, or ``None`` if it cannot be read.

    The runtime has always applied this — it meets the session's mode with the profile's
    before enforcing anything — but the control plane did not, so a record could say `auto`
    while the session it described ran under `default`. That gap was invisible while the mode
    was fixed at creation and nobody could ask for it again; it is not invisible now that a
    person changes it from a chip and watches for the answer. Applied here so what is recorded,
    what is reported and what is enforced are the same value.
    """
    from frank.hub.services.agents import _agent_configuration_for_request

    try:
        _, configuration = _agent_configuration_for_request(agent, working_directory)
    except Exception:  # noqa: BLE001 — an unreadable profile clamps nothing rather than failing
        logger.debug("could not read the permission ceiling of agent %s", agent, exc_info=True)
        return None
    return configuration.permission_policy


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

    configured = getattr(getattr(state.global_configuration, "agent", None), "permission_mode", "")

    working_directory = str(params.get("working_directory") or "")
    if parent is not None and not working_directory:
        working_directory = parent.working_directory

    try:
        mode = PermissionMode.child_of(
            parent.permission_mode if parent is not None else None,
            requested=params.get("permission_mode"),
            fallback=configured,
            # The agent's own ceiling, which the runtime applies whether or not this record
            # mentions it — so it is applied here too rather than leaving the record claiming a
            # mode the session will not run under.
            ceiling=_agent_permission_ceiling(agent, working_directory),
        )
    except ValueError as conflict:
        # A session that cannot answer a gate, asking for a peer that raises them. Refused at
        # creation, where it can be reported to whoever asked, rather than minting a peer that
        # would park on its first gate and be waited on forever.
        raise RpcError(str(conflict), status_code=409, code="unattended_conflict") from conflict
    # Read-only is a confinement, not a policy: a session that may look and not touch is one
    # whose profile has nowhere writable. A child inherits it, because a session that cannot
    # write must not be able to create a peer that can.
    read_only = bool(params.get("read_only")) or bool(
        parent is not None and not (parent.sandbox or {}).get("filesystem", {}).get("writable")
    )
    sandbox = _resolve_sandbox(agent, working_directory, parent, read_only)

    # `create` registers the session in memory; the durable write is awaited here, off the
    # loop, because the worker about to be started will look this row up.
    record = state.registry.create(
        agent=agent,
        working_directory=working_directory,
        permission_mode=str(mode),
        sandbox=sandbox,
        workspace_id=str(params.get("workspace_id") or (parent.workspace_id if parent else "")),
        parent=parent_id,
        title=str(params.get("title") or ""),
        created_at=_now(),
    )

    # Where the session will actually run, and its durable row, decided here rather than on
    # its first turn: the workspace strategy can put a session in its own git worktree, and a
    # session whose tools do not yet know which directory they operate on is not a session
    # anyone can safely message. It is also the row the title and the draft later land on.
    from frank.hub.services.sessions import _ensure_session_workspace

    try:
        workspace = await asyncio.to_thread(
            _ensure_session_workspace,
            record.id,
            record.agent,
            record.working_directory,
            str(params.get("worktree_strategy") or ""),
            record.permission_mode,
            record.workspace_id,
        )
        state.registry.mark(record.id, runtime_working_directory=workspace.runtime_working_directory)
    except Exception:  # noqa: BLE001 — a workspace that cannot be prepared is not a fatal
        logger.exception("could not prepare a workspace for session %s", record.id)
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


async def _waiting_on(session_id: str) -> str:
    """What a session parked on a human is parked on, as a sentence, or ``""``.

    `awaiting_input: true` says a session is blocked; it does not say what would unblock it, and
    a caller cannot act on the difference. A peer reading that about a session it created could
    not tell "parked on a permission request, working fine, leave it" from "never started" — and
    reading it as the latter is what led one to replace three peers that were mid-review.
    """
    if state.turn_store is None:
        return ""
    try:
        for task in await state.turn_store.turns_for_session(session_id):
            pending = TurnRecord.from_metadata(task.metadata).pending
            if pending is None or not pending.gates:
                continue
            unanswered = [gate for gate in pending.gates if gate.request_id not in pending.answers]
            if not unanswered:
                continue
            gate = unanswered[0]
            if gate.is_question:
                return "a question it asked the user"
            command = (gate.command or "").strip()
            return f"a permission decision for `{command}`" if command else "a permission decision"
    except Exception:  # noqa: BLE001 — a record that cannot be read is not a reason to fail the call
        logger.debug("could not read what %s is waiting on", session_id, exc_info=True)
    return ""


async def _session_get(params: dict) -> dict:
    record = _session(_require(params, "id"))
    payload = _public(record)
    if payload.get("awaiting_input"):
        waiting_on = await _waiting_on(record.id)
        if waiting_on:
            payload["waiting_on"] = waiting_on
    return {"session": payload}


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


async def _tell_worker_permission_mode(record: SessionRecord) -> None:
    """Push a record's mode down to its worker, if it has one right now.

    Deliberately not `wake_then_relay`: a sleeping session has nothing to tell. Its next
    worker is forked from the record, which already carries the new mode, so waking one only
    to inform it would spend a process on a message it did not need."""
    if record.asleep or not record.is_live:
        return
    with contextlib.suppress(Exception):  # a worker mid-teardown simply reads it on the next fork
        await state.relay_to_session(record, "session/permission-mode", {
            "permission_mode": record.permission_mode,
        })


async def _session_permission_mode(params: dict) -> dict:
    """Change the permission mode a session runs under, while it runs.

    The mode was fixed at `create` for the whole of a session's life, and the cost of that was
    paid by the person: a conversation begun under manual approvals and then trusted had to be
    abandoned and restarted to stop being asked about every command, and one begun under `auto`
    could not be reined in without ending it. So the mode is a live property now, and the two
    guarantees that made it worth fixing are kept as clamps rather than as immobility:

    - **A child is never looser than its parent.** The requested mode is met against the
      parent's, exactly as at creation.
    - **Tightening reaches everything underneath.** Restricting a session restricts the whole
      subtree it created, because a child that stayed loose would be a way to keep the old
      authority alive under a session that has just given it up.

    Not a verb a session may call (it is absent from `_SESSION_CALLER_METHODS`), which is the
    part that matters: this is the human's control, and a model must not be able to widen the
    policy it is being judged by — its own, or one of its children's.
    """
    assert state.registry is not None
    record = _session(_require(params, "id"))
    if not record.is_live:
        raise RpcError(
            f"Session {record.id} has ended, so its permission mode cannot be changed.",
            status_code=409,
            code="session_not_running",
        )
    requested = PermissionMode.parse(params.get("permission_mode"))
    if requested is None:
        raise RpcError(
            "permission_mode must be one of: ask, auto.",
            status_code=400,
            code="invalid_permission_mode",
        )
    parent = state.registry.get(record.parent) if record.parent else None
    mode = PermissionMode.more_restrictive(
        requested,
        parent.permission_mode if parent is not None else None,
        _agent_permission_ceiling(record.agent, record.working_directory),
    )
    changed = [record] if record.permission_mode != str(mode) else []
    state.registry.mark(record.id, permission_mode=str(mode), updated_at=_now())
    for descendant in state.registry.descendants_of(record.id):
        if not descendant.is_live:
            continue
        clamped = PermissionMode.more_restrictive(descendant.permission_mode, mode)
        if descendant.permission_mode == str(clamped):
            continue
        state.registry.mark(descendant.id, permission_mode=str(clamped), updated_at=_now())
        changed.append(descendant)
    for altered in changed:
        await _tell_worker_permission_mode(altered)
    if changed:
        state.broadcaster.publish({"type": "sessions_changed"})
    return {
        "id": record.id,
        "permission_mode": str(mode),
        # What the caller asked for is not always what it got: the parent clamp is applied
        # here, and a creator that cannot see the difference cannot reason about it.
        "clamped": str(mode) != str(requested),
        "descendants_changed": [altered.id for altered in changed if altered.id != record.id],
    }


async def _session_send(params: dict) -> dict:
    """Relay a message to the session's own socket.

    A message to a session that is mid-turn is injected at its next safe point rather than
    queued behind the whole turn, which is what makes a peer's question reach a working
    session instead of waiting for it to finish."""
    record = _session(_require(params, "id"))
    if not record.is_live:
        raise RpcError(
            f"Session {record.id} has ended, so it cannot accept messages.",
            status_code=409,
            code="session_not_running",
        )
    return await state.wake_then_relay(record, "message/send", params)


async def _turn_cancel(params: dict) -> dict:
    record = _session(_require(params, "id"))
    return await state.wake_then_relay(record, "tasks/cancel", params)


async def _session_respond(params: dict) -> dict:
    """Answer a session's pending human-in-the-loop gate."""
    record = _session(_require(params, "id"))
    _require(params, "request_id")
    return await state.wake_then_relay(record, "input/respond", params)


async def _session_compact(params: dict) -> dict:
    """Ask a session to compact its own conversation."""
    record = _session(_require(params, "id"))
    return await state.wake_then_relay(record, "session/compact", params)


async def _session_goal_clear(params: dict) -> dict:
    """Call off a session's goal, because the person asked. The session stops opening turns for
    it; whatever turn is in flight finishes on its own."""
    record = _session(_require(params, "id"))
    return await state.wake_then_relay(record, "session/goal-clear", params)


async def _jobs_list(params: dict) -> dict:
    """What background work a session has in flight. Read from the session rather than the
    store: a background job lives in the process running it."""
    record = _session(_require(params, "id"))
    return await state.wake_then_relay(record, "jobs/list", params)


async def _jobs_detach(params: dict) -> dict:
    """Detach a still-blocking command so the session's turn can continue without it."""
    record = _session(_require(params, "id"))
    _require(params, "tool_call_id")
    return await state.wake_then_relay(record, "jobs/detach", params)


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

    Listed apart from sessions because they are a different kind of thing: Frank does not own
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
        async for event in manager.message_session(name, message):
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
    # The prototype's own numbers, asked for rather than remembered. `threads` and
    # `frozen_objects` are here because both are invariants that fail silently: a prototype
    # that has picked up a second thread cannot fork safely, and one whose heap was never
    # frozen still works while costing most of the memory saving. Neither is visible anywhere
    # else, so this is where they get reported.
    prototype = await state.prototype.refresh_status() if state.prototype else {
        "alive": False, "pid": 0, "threads": 0, "frozen_objects": 0, "sessions": 0,
    }
    return {
        "ok": True,
        "sessions": {"live": len(live), "total": len(state.registry.all())},
        "prototype": prototype,
        "socket": str(state.daemon_socket),
        "port": state.daemon_port,
        # Which image is actually serving. Once the daemon is installed there are two `frank`
        # on a developer's PATH — the signed bundle and the checkout's `uv run frank` — and they
        # share a runtime directory, so whichever started first owns it. That is invisible
        # otherwise, and it decides whether computer control has a stable Accessibility grant:
        # the frozen image is one code identity across rebuilds, an interpreter is not.
        "image": {"executable": sys.executable, "frozen": bool(getattr(sys, "frozen", False))},
    }


async def _daemon_restart(_params: dict) -> dict:
    """Replace this daemon with a fresh one, and say what that costs.

    It exists for one reason: macOS caches the Accessibility trust check per process, so a
    daemon that was already running when the user granted the permission never sees it, and its
    workers are re-execs of it, so neither do they. The desktop app used to get this for free by
    killing the daemon it owned and relaunching itself. It no longer owns one, so the daemon has
    to be able to do it on request.

    **Sessions survive it.** They used to not: the registry lived in memory, so a restart took
    every session with it and this method's job included warning about that. The registry is
    durable now, so a restart ends every session's *process* and no session at all — each live
    one comes back asleep and the next message to it forks a worker. `sessions_slept` is
    returned so a caller can say what actually happens, which is much less than it was.

    The re-exec is scheduled rather than immediate so this response reaches the client first —
    otherwise the caller sees a dropped connection and cannot tell success from a crash."""
    assert state.registry is not None
    running = len(state.registry.running())

    async def replace() -> None:
        # `execv` rather than spawn-and-exit, for two reasons. It keeps the pid, so the lock
        # file's descriptor carries over and a successor never races the predecessor for it —
        # the failure mode a naive stop-then-start hits, where the new daemon dies on a lock the
        # old one has not released yet and nothing is left running. And it replaces the address
        # space, which is where the Accessibility trust result was cached, so the successor asks
        # the current TCC database rather than remembering the old answer. That second point is
        # the whole purpose of this method and can only be confirmed on macOS.
        #
        # The sleep is long enough for the response to be written and flushed, short enough that
        # nobody is left wondering.
        await asyncio.sleep(0.5)
        if state.lifecycle is not None:
            # Sleep them rather than reap them. Their records are durable, so stopping the
            # processes is the whole of what a restart has to do — and the successor picks
            # every one of them back up as an asleep session.
            with contextlib.suppress(Exception):
                await state.lifecycle.sleep_all()
        os.execv(sys.executable, [sys.executable, *_daemon_argv()])

    asyncio.get_running_loop().create_task(replace())
    return {"restarting": True, "sessions_slept": running}


def _daemon_argv() -> list[str]:
    """How to re-enter this program as the daemon.

    Mirrors `prototype.prototype_command`: in the frozen application the executable *is* the
    image and takes the entry point as its first argument, while from a checkout it is an
    interpreter that needs `-m frank` first. Getting this wrong would re-exec into the CLI,
    which exits."""
    if getattr(sys, "frozen", False):
        return ["frankd"]
    return ["-m", "frank", "frankd"]


async def _workspace_list(params: dict) -> dict:
    """Every workspace and its locations. On the control plane because the CLI needs to turn a
    path into a workspace id, and the CLI does not speak to the REST app."""
    from frank.hub.services.workspaces import _workspaces_payload

    return await asyncio.to_thread(_workspaces_payload)


async def _schedule_create(params: dict) -> dict:
    """Write down a recurring prompt. Validated here rather than at the first firing, because
    the first firing may be days away and unattended."""
    from frank.hub.services import schedules

    try:
        return await asyncio.to_thread(
            schedules.create,
            workspace_id=str(params.get("workspace_id") or ""),
            name=_require(params, "name"),
            cron=_require(params, "cron"),
            prompt=_require(params, "prompt"),
            agent=_require(params, "agent"),
            permission_mode=str(params.get("permission_mode") or ""),
            timezone_name=str(params.get("timezone") or ""),
            working_directory=str(params.get("working_directory") or ""),
        )
    except schedules.ScheduleError as error:
        raise RpcError(str(error), code="invalid_schedule") from None


async def _schedule_list(params: dict) -> dict:
    from frank.hub.services import schedules

    listing = await asyncio.to_thread(schedules.listing, str(params.get("workspace_id") or ""))
    return {"schedules": listing}


async def _schedule_get(params: dict) -> dict:
    from frank.hub.services import schedules

    try:
        return await asyncio.to_thread(schedules.get, _require(params, "id"))
    except schedules.ScheduleError as error:
        raise RpcError(str(error), status_code=404, code="no_such_schedule") from None


async def _schedule_enable(params: dict) -> dict:
    from frank.hub.services import schedules

    try:
        return await asyncio.to_thread(
            schedules.set_enabled, _require(params, "id"), bool(params.get("enabled", True)))
    except schedules.ScheduleError as error:
        raise RpcError(str(error), status_code=404, code="no_such_schedule") from None


async def _schedule_delete(params: dict) -> dict:
    from frank.hub.services import schedules

    schedule_id = _require(params, "id")
    try:
        await asyncio.to_thread(schedules.delete, schedule_id)
    except schedules.ScheduleError as error:
        raise RpcError(str(error), status_code=404, code="no_such_schedule") from None
    return {"deleted": schedule_id}


async def _schedule_run(params: dict) -> dict:
    """Fire one now without moving its window — the only way to find out the agent name was
    wrong before six tomorrow morning."""
    from frank.daemon import scheduler
    from frank.hub import state as hub_state
    from frank.hub.database import ScheduleRecord
    from frank.hub.services import schedules

    schedule_id = _require(params, "id")
    database_session = hub_state.session_factory()
    try:
        record = database_session.get(ScheduleRecord, schedule_id)
        if record is None:
            raise RpcError(f"No schedule {schedule_id!r}.", status_code=404, code="no_such_schedule")
        database_session.expunge(record)
    finally:
        database_session.close()
    await scheduler._fire(record)
    return await asyncio.to_thread(schedules.get, schedule_id)


METHODS: dict[str, Callable[[dict], Awaitable[dict]]] = {
    "session.create": _session_create,
    "session.list": _session_list,
    "session.get": _session_get,
    "session.tree": _session_tree,
    "session.end": _session_end,
    "session.permission_mode": _session_permission_mode,
    "session.send": _session_send,
    "turn.cancel": _turn_cancel,
    "session.respond": _session_respond,
    "session.compact": _session_compact,
    "session.goal_clear": _session_goal_clear,
    "jobs.list": _jobs_list,
    "jobs.detach": _jobs_detach,
    "session.history": _session_history,
    "remote.list": _remote_list,
    "remote.send": _remote_send,
    "turn.get": _turn_get,
    "daemon.status": _daemon_status,
    "daemon.restart": _daemon_restart,
    "workspace.list": _workspace_list,
    "schedule.create": _schedule_create,
    "schedule.list": _schedule_list,
    "schedule.get": _schedule_get,
    "schedule.enable": _schedule_enable,
    "schedule.delete": _schedule_delete,
    "schedule.run": _schedule_run,
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


@router.post("/telemetry/faults")
async def telemetry_faults(request: Request) -> JSONResponse:
    """Where the interface reports a fault it handled and carried on past.

    The browser cannot reach the collector itself — the OTLP endpoint and its headers are
    configuration that lives in this process, and a webview holding either would mean
    credentials in a page and a CORS negotiation with someone else's backend. So it reports
    here and this forwards, through the exporter already carrying traces and metrics.

    Always 202, and deliberately: a client must never retry, escalate, or show a person
    anything because its *telemetry* did not land. When telemetry is switched off — the
    default — `record_client_fault` is a no-op and this quietly discards, which is the same
    answer the rest of the harness gives.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"accepted": False}, status_code=202)
    if not isinstance(payload, dict):
        return JSONResponse({"accepted": False}, status_code=202)
    # Whole, not clipped. These were cut to 200, 2000, 500 and 100 characters, and the one that
    # mattered was `detail`: it carries a stack trace, a trace is longest exactly when the fault
    # is least understood, and 2000 characters reliably kept the frames nearest the throw while
    # discarding the ones that said which of the caller's paths reached it. A fault is rare and a
    # log line is cheap; a truncated one costs another reproduction.
    # Two fields, not a sentence with the place glued to the front. The interface used to send
    # `chat input: could not read the message history`, which meant the only way to ask "which
    # surface is failing" was to match on a prefix — and a colon inside a message took that
    # apart wrongly. `component` and `operation` are dimensions; they group.
    component = str(payload.get("component") or "")
    operation = str(payload.get("operation") or "")
    # The error arrives already parsed into fields — the interface runs whatever it caught
    # through `serialize-error`, so a thrown string or bare object has a name and a message
    # like anything else. Nothing here has to guess at the shape of a blob.
    error_name = str(payload.get("errorName") or "")
    error_message = str(payload.get("errorMessage") or "")
    error_stack = str(payload.get("errorStack") or "")
    url = str(payload.get("url") or "")
    session_id = str(payload.get("sessionId") or "")
    # Logged whether or not telemetry is configured, and that is the point: the interface no
    # longer keeps a console copy, so this log is the single answer to "where did that go".
    # Telemetry, when on, is an additional destination rather than the only one.
    #
    # As fields rather than as a sentence. This used to read `interface fault at %s: %s -- %s`,
    # which glued the page, the context and a stack trace together with punctuation invented
    # here and nowhere else — so anything reading the log back, a person included, had to take
    # it apart by counting colons, and a `--` inside a stack trace took it apart wrongly. The
    # same reasoning already applies to every payload this harness puts in front of a model:
    # the fields have names, so use them.
    logger.warning("interface fault %s", compact({
        "component": component,
        "operation": operation,
        "error": error_name,
        "message": error_message,
        "url": url,
        "session": session_id,
        "stack": error_stack,
    }))
    telemetry.record_client_fault(
        component,
        operation,
        {
            "frank.client.error.name": error_name,
            "frank.client.error.message": error_message,
            "frank.client.error.stack": error_stack,
            "frank.client.url": url,
            "frank.client.session_id": session_id,
        },
    )
    return JSONResponse({"accepted": True}, status_code=202)


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
    # Who is calling, according to the kernel and the token — never according to the body. The
    # key is stripped before anything reads it, so `calling_session` inside a handler can only
    # ever be what the middleware put there; a caller cannot name itself.
    params.pop("calling_session", None)
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
        logger.exception("control-plane call %s failed", method)
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
                # One part, not one message: the bus carries parts as the model emits them,
                # so a turn's prose arrives as a run of text parts rather than a finished
                # message. Naming the field `message` cost the interface every live update —
                # the client's reducers all walk `.parts`, which a part does not have, so
                # each frame reduced to nothing and answers only appeared on reload.
                yield {"data": compact({"kind": "live", "seq": event.get("seq", 0), "part": event.get("part")})}
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
                    # is waiting on the daemon.
                    break
                yield {"data": compact(event)}
        finally:
            state.broadcaster.unsubscribe(subscription)

    return EventSourceResponse(stream())
