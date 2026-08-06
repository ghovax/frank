"""The daemon's control plane: one method surface, reached over a unix socket and a loopback port."""

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

from langmesh.base.permission_mode import PermissionMode
from langmesh.base.tuning import Tunable, active_tuning
from langmesh.base import telemetry
from langmesh.daemon import state
from langmesh.daemon.registry import SessionRecord
from langmesh.protocol.turn_record import TurnRecord
from langmesh.base.serialization import compact

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


def _inherited_conversation(params: dict) -> list[dict[str, Any]]:
    """Validate a model-facing conversation snapshot supplied for a child session."""
    inherited_conversation = params.get("inherited_conversation")
    if inherited_conversation is None:
        return []
    if not isinstance(inherited_conversation, list) or not all(
        isinstance(message, dict) for message in inherited_conversation
    ):
        raise RpcError("inherited_conversation must be a list of serialized messages")
    return inherited_conversation


def _session(session_id: str) -> SessionRecord:
    record = state.registry.get(session_id) if state.registry else None
    if record is None:
        raise RpcError(f"No session {session_id!r}.", status_code=404, code="no_such_session")
    return record


def _assert_session_known(session_id: str) -> None:
    """Refuse an id nothing has heard of, since empty and unknown look identical to a caller."""
    if state.registry is not None and state.registry.get(session_id) is not None:
        return
    raise RpcError(f"No session {session_id!r}.", status_code=404, code="no_such_session")


def _assert_agent_exists(agent: str, working_directory: str) -> None:
    """Refuse a session for a profile that is not there, rather than failing on its first message."""
    from langmesh.commons.services.agents import available_agent_names

    configuration = state.global_configuration
    if configuration is None:
        return
    directories = (
        configuration.agent_directories_for(working_directory)
        if working_directory
        else configuration.agent_directories()
    )
    available = available_agent_names(directories)
    if agent not in available:
        known = ", ".join(sorted(available)) or "none found"
        raise RpcError(
            f"No agent profile named {agent!r}. Available: {known}.",
            status_code=404,
            code="no_such_agent",
        )


def _public(record: SessionRecord) -> dict:
    """A session as a client sees it, with `busy` set from what is actually mid-turn before activity is derived."""
    record.busy = record.id in state._running_contexts
    return {**record.public(), "goal": state._session_goals.get(record.id)}


def _resolve_sandbox(agent: str, working_directory: str, parent, read_only: bool = False) -> dict:
    """The confinement a new session gets: the machine's, narrowed by the agent's, clamped by its creator's."""
    from langmesh.base import confinement
    from langmesh.commons.services.agents import _agent_configuration_for_request

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
    # Read-only at the kernel: a profile with nowhere writable, which no grant can widen.
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
    """The loosest mode the agent's profile allows, or ``None`` when it cannot be read."""
    from langmesh.commons.services.agents import _agent_configuration_for_request

    try:
        _, configuration = _agent_configuration_for_request(agent, working_directory)
    except Exception:  # noqa: BLE001 — an unreadable profile clamps nothing rather than failing
        logger.debug("could not read the permission ceiling of agent %s", agent, exc_info=True)
        return None
    return configuration.permission_policy


async def _session_create(params: dict) -> dict:
    """Mint a session and hand back its handle. The only place a session's configuration is set."""
    assert state.registry is not None and state.lifecycle is not None
    # No fallback: which agent runs is the one thing nothing can guess on the caller's behalf.
    agent = _require(params, "agent")
    _assert_agent_exists(agent, str(params.get("working_directory") or ""))
    # A session that authenticated as itself is the parent, whatever it asked for, so the clamp is not opt-out.
    parent_id = str(params.get("calling_session") or params.get("parent") or "").strip()
    parent = state.registry.get(parent_id) if parent_id else None
    if parent_id and parent is None:
        raise RpcError(f"No parent session {parent_id!r}.", status_code=404, code="no_such_session")
    inherited_conversation = _inherited_conversation(params)
    if inherited_conversation and parent is None:
        raise RpcError("A conversation can only be inherited from a parent session.")
    if inherited_conversation and state.turn_store is None:
        raise RpcError(
            "The conversation store is unavailable.", status_code=503, code="store_unavailable",
        )

    configured = getattr(getattr(state.global_configuration, "agent", None), "permission_mode", "")

    working_directory = str(params.get("working_directory") or "")
    if parent is not None and not working_directory:
        working_directory = parent.working_directory

    try:
        mode = PermissionMode.child_of(
            parent.permission_mode if parent is not None else None,
            requested=params.get("permission_mode"),
            fallback=configured,
            # The agent's ceiling, applied here too, so the record cannot claim a mode the session will not run under.
            ceiling=_agent_permission_ceiling(agent, working_directory),
        )
    except ValueError as conflict:
        # A session that cannot answer a gate asking for a peer that raises them: refused at creation.
        raise RpcError(str(conflict), status_code=409, code="unattended_conflict") from conflict
    # Read-only is a confinement, and a child inherits it: one that cannot write must not create one that can.
    read_only = bool(params.get("read_only")) or bool(
        parent is not None and not (parent.sandbox or {}).get("filesystem", {}).get("writable")
    )
    sandbox = _resolve_sandbox(agent, working_directory, parent, read_only)

    # `create` registers in memory; the durable write is awaited because the worker will look this row up.
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

    if inherited_conversation:
        try:
            await state.turn_store.seed_inherited_conversation(record.id, inherited_conversation)
        except Exception as exception:
            logger.exception("could not seed inherited conversation for session %s", record.id)
            state.registry.forget(record.id)
            with contextlib.suppress(Exception):
                await state.turn_store.delete_session(record.id)
            raise RpcError(
                "The parent conversation could not be inherited.",
                status_code=503,
                code="conversation_inheritance_failed",
            ) from exception

    # Where the session will run, decided here: a worktree strategy puts its tools somewhere else.
    from langmesh.commons.services.sessions import _ensure_session_workspace

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
    # The token is returned exactly once. The parent and mode come back because either may differ.
    return {
        "id": record.id,
        "token": record.token,
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
    """What a parked session is parked on, as a sentence, since "blocked" alone cannot be acted on."""
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
    """A session and everything under it, so a fan-out renders as a hierarchy rather than a pile."""
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


async def _tell_session_permission_mode(record: SessionRecord) -> None:
    """Push a mode into a hosted session. A sleeping one needs none: its next executor reads the record."""
    if record.asleep or not record.is_live:
        return
    with contextlib.suppress(Exception):  # a session mid-teardown simply reads it when it is next built
        await state.relay_to_session(record, "session/permission-mode", {
            "permission_mode": record.permission_mode,
        })


async def _session_permission_mode(params: dict) -> dict:
    """Change the mode a session runs under, while it runs, rather than making it something only `create` sets."""
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
            "permission_mode must be one of: ask, automatic.",
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
        await _tell_session_permission_mode(altered)
    if changed:
        state.broadcaster.publish({"type": "sessions_changed"})
    return {
        "id": record.id,
        "permission_mode": str(mode),
        # What the caller asked for is not always what it got, and a creator that cannot see the clamp cannot reason.
        "clamped": str(mode) != str(requested),
        "descendants_changed": [altered.id for altered in changed if altered.id != record.id],
    }


async def _session_send(params: dict) -> dict:
    """Relay a message to the session's socket, injected at its next safe point rather than queued behind the turn."""
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
    """Call off a session's goal; a turn already in flight finishes on its own."""
    record = _session(_require(params, "id"))
    return await state.wake_then_relay(record, "session/goal-clear", params)


async def _jobs_list(params: dict) -> dict:
    """What background work a session has in flight, read from the executor hosting it."""
    record = _session(_require(params, "id"))
    return await state.wake_then_relay(record, "jobs/list", params)


async def _jobs_detach(params: dict) -> dict:
    """Detach a still-blocking command so the session's turn can continue without it."""
    record = _session(_require(params, "id"))
    _require(params, "tool_call_id")
    return await state.wake_then_relay(record, "jobs/detach", params)


async def _session_history(params: dict) -> dict:
    """A session's turns from the store, so history reads whether the session is running, parked or reaped."""
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
    """The peers registered on other hosts, listed apart because LangMesh owns neither their lifecycle nor their history."""
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
    """Hand one message to a remote peer and return what it produced. One-shot: a different bargain from a local one."""
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
    """The prose in one streamed event, whether it arrived as a Message or as a Task's artifacts."""
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
    hosted = state.host.hosted() if state.host is not None else []
    return {
        "ok": True,
        "sessions": {"live": len(live), "total": len(state.registry.all()), "hosted": len(hosted)},
        "socket": str(state.daemon_socket),
        "port": state.daemon_port,
        # Which image is serving, since two `langmesh` can share a runtime directory and the first one owns it.
        "image": {"executable": sys.executable, "frozen": bool(getattr(sys, "frozen", False))},
    }


async def _daemon_restart(_params: dict) -> dict:
    """Replace this daemon with a fresh one, since macOS caches the Accessibility trust check per process."""
    assert state.registry is not None
    hosted = len(state.registry.hosted_records())

    async def replace() -> None:
        # `execv` rather than spawn-and-exit: it keeps the pid, so no successor races the lock file.
        await asyncio.sleep(0.5)
        if state.lifecycle is not None:
            # Sleep them rather than reap them: their records are durable, and the successor picks each back up.
            with contextlib.suppress(Exception):
                await state.lifecycle.sleep_all()
        os.execv(sys.executable, [sys.executable, *_daemon_argv()])

    asyncio.get_running_loop().create_task(replace())
    return {"restarting": True, "sessions_slept": hosted}


def _daemon_argv() -> list[str]:
    """How to re-enter this program as the daemon, frozen or from a checkout."""
    if getattr(sys, "frozen", False):
        return ["langmeshd"]
    return ["-m", "langmesh", "langmeshd"]


async def _workspace_list(params: dict) -> dict:
    """Every workspace and its locations, here because the CLI turns a path into an id and speaks no REST."""
    from langmesh.commons.services.workspaces import _workspaces_payload

    return await asyncio.to_thread(_workspaces_payload)


async def _schedule_create(params: dict) -> dict:
    """Write down a recurring prompt, validated now rather than at a first firing days away."""
    from langmesh.commons.services import schedules

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
    from langmesh.commons.services import schedules

    listing = await asyncio.to_thread(schedules.listing, str(params.get("workspace_id") or ""))
    return {"schedules": listing}


async def _schedule_get(params: dict) -> dict:
    from langmesh.commons.services import schedules

    try:
        return await asyncio.to_thread(schedules.get, _require(params, "id"))
    except schedules.ScheduleError as error:
        raise RpcError(str(error), status_code=404, code="no_such_schedule") from None


async def _schedule_enable(params: dict) -> dict:
    from langmesh.commons.services import schedules

    try:
        return await asyncio.to_thread(
            schedules.set_enabled, _require(params, "id"), bool(params.get("enabled", True)))
    except schedules.ScheduleError as error:
        raise RpcError(str(error), status_code=404, code="no_such_schedule") from None


async def _schedule_delete(params: dict) -> dict:
    from langmesh.commons.services import schedules

    schedule_id = _require(params, "id")
    try:
        await asyncio.to_thread(schedules.delete, schedule_id)
    except schedules.ScheduleError as error:
        raise RpcError(str(error), status_code=404, code="no_such_schedule") from None
    return {"deleted": schedule_id}


async def _schedule_run(params: dict) -> dict:
    """Fire one now without moving its window, so a wrong agent name is found before tomorrow morning."""
    from langmesh.daemon import scheduler
    from langmesh.commons import state as commons_state
    from langmesh.commons.database import ScheduleRecord
    from langmesh.commons.services import schedules

    schedule_id = _require(params, "id")
    database_session = commons_state.session_factory()
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


#: What a session may ask on its own behalf: a capability for its work, not a second daemon token.
_SESSION_CALLER_METHODS = frozenset({
    "session.create", "session.send", "session.get", "session.tree",
    "session.end", "session.history", "remote.list", "remote.send",
})


def _refuse_session_caller(caller: str, method: str, params: dict) -> Optional[RpcError]:
    """Whether an attributed session may make this call: only its own verbs, and only within its own subtree."""
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
    """Where the interface reports a handled fault, since a webview cannot hold the collector's credentials."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"accepted": False}, status_code=202)
    if not isinstance(payload, dict):
        return JSONResponse({"accepted": False}, status_code=202)
    # Whole, not clipped: a trace is longest exactly when the fault is least understood.
    component = str(payload.get("component") or "")
    operation = str(payload.get("operation") or "")
    # The error arrives already parsed into fields, so nothing here guesses at the shape of a blob.
    error_name = str(payload.get("errorName") or "")
    error_message = str(payload.get("errorMessage") or "")
    error_stack = str(payload.get("errorStack") or "")
    url = str(payload.get("url") or "")
    session_id = str(payload.get("sessionId") or "")
    # Logged whether or not telemetry is on, since this is the single answer to where a fault went.
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
            "langmesh.client.error.name": error_name,
            "langmesh.client.error.message": error_message,
            "langmesh.client.error.stack": error_stack,
            "langmesh.client.url": url,
            "langmesh.client.session_id": session_id,
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
    # Who is calling, per the kernel and the token, never the body: a caller cannot name itself.
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
    """Watch a session: a snapshot of what happened, then everything as it happens."""
    _session(session_id)
    # Same scoping as the control plane: a session watches its own subtree, a person's client is not narrowed.
    caller = getattr(request.state, "calling_session", "")
    if caller:
        refusal = _refuse_session_caller(caller, "session.get", {"id": session_id})
        if refusal is not None:
            raise refusal

    async def stream():
        assert state.turn_store is not None
        subscription = state.event_bus.subscribe(session_id)
        try:
            # The newest page, not the whole conversation: a long one costs most of a second to read,
            # revalidate and re-encode, and the client asks for what came before it when somebody scrolls.
            page = await state.turn_store.turn_page_for_session(
                session_id, limit=active_tuning().amount(Tunable.attach_snapshot_rows)
            )
            yield {
                "data": compact({
                    "kind": "snapshot",
                    "turns": [
                        turn.model_dump(by_alias=True, exclude_none=True, mode="json")
                        if hasattr(turn, "model_dump") else turn
                        for turn in (page.get("turns") or [])
                    ],
                    "has_more": bool(page.get("has_more")),
                    "next_before_row_id": page.get("next_before_row_id"),
                })
            }
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(subscription.get(), timeout=15)
                except asyncio.TimeoutError:
                    # A comment keeps the connection warm through proxies without inventing an event to ignore.
                    yield {"comment": "keepalive"}
                    continue
                if event is None:
                    yield {"data": compact({"kind": "done"})}
                    break
                if "turn" in event:
                    # A turn started or ended — distinct from `done`, which is the session itself ending.
                    yield {"data": compact({
                        "kind": "turn",
                        "seq": event.get("seq", 0),
                        "running": bool((event.get("turn") or {}).get("running")),
                    })}
                    continue
                # One part, not one message: the bus carries parts as the model emits them.
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
                    # The bus is closed and the daemon is going down: a draining server cannot outwait this stream.
                    break
                yield {"data": compact(event)}
        finally:
            state.broadcaster.unsubscribe(subscription)

    return EventSourceResponse(stream())
