"""Terminals routes."""

from __future__ import annotations
from frank.hub.brokers.terminals import TerminalSession, _delete_terminal_state, _list_terminal_states, _terminal_context_for_request, _terminal_directory
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from contextlib import suppress
from pathlib import Path
import asyncio
import json
import logging
from frank.hub import state

logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/terminals")
async def list_terminals(session_id: str = "", working_directory: str = ""):
    terminal_context = await _terminal_context_for_request(session_id, working_directory)
    persisted = await asyncio.to_thread(_list_terminal_states, terminal_context)
    live_keys = state.terminal_manager.live_keys(terminal_context) if state.terminal_manager is not None else set()
    terminals = [
        {"terminal_key": entry["terminal_key"], "cwd": entry["working_directory"], "running": entry["terminal_key"] in live_keys}
        for entry in persisted
    ]
    # A freshly opened terminal is live before it has persisted any scrollback; surface
    # it too so its tab does not vanish on a mid-session refresh.
    known = {entry["terminal_key"] for entry in persisted}
    for terminal_key in sorted(live_keys - known):
        terminals.append({"terminal_key": terminal_key, "cwd": "", "running": True})
    return {"terminals": terminals}


@router.delete("/terminals/{terminal_key}")
async def delete_terminal(terminal_key: str, session_id: str = "", working_directory: str = ""):
    terminal_key = (terminal_key or "").strip()[:128]
    if not terminal_key:
        raise HTTPException(status_code=400, detail="A terminal key is required.")
    terminal_context = await _terminal_context_for_request(session_id, working_directory)
    if state.terminal_manager is not None:
        await state.terminal_manager.close_one(terminal_context, terminal_key)
    await asyncio.to_thread(_delete_terminal_state, terminal_context, terminal_key)
    return {"ok": True}


@router.websocket("/terminal")
async def terminal_websocket(
    websocket: WebSocket,
    session_id: str = "",
    working_directory: str = "",
    terminal_key: str = "main",
    # A remote-location terminal: an SSH login shell on this host, in `location_base_directory`.
    # When empty, the terminal is local (working_directory as before).
    location_kind: str = "local",
    location_base_directory: str = "",
    location_host_alias: str = "",
    rows: int = 24,
    columns: int = 80,
):
    terminal_key = (terminal_key or "main").strip()[:128] or "main"
    await websocket.accept()
    if state.terminal_manager is None:
        await websocket.send_json({
            "type": "error",
            "code": "terminal_unavailable",
            "message": "Terminal service is not available.",
            "recoverable": True,
        })
        await websocket.close()
        return
    subscriber: asyncio.Queue | None = None
    session: TerminalSession | None = None
    try:
        remote_alias = location_host_alias.strip() if location_kind == "remote" else ""
        if remote_alias and location_base_directory.strip():
            # Remote terminal: the directory is the remote base dir (used verbatim in the
            # ssh `cd`, never touched locally), and the session sshes to the host alias.
            directory = Path(location_base_directory.strip())
        elif location_base_directory.strip():
            directory = Path(location_base_directory.strip()).expanduser()
        else:
            directory = _terminal_directory(session_id, working_directory)
        session = await state.terminal_manager.get_or_create(session_id, directory, rows, columns, terminal_key=terminal_key, remote_host_alias=remote_alias)
        subscriber = session.subscribe()
        await websocket.send_json({
            "type": "ready",
            "cwd": str(session.directory),
            "persistent": True,
            "terminal_key": session.terminal_key,
            "pid": session.pid,
            "rows": session.rows,
            "columns": session.columns,
        })

        async def output_loop() -> None:
            assert subscriber is not None
            while True:
                event = await subscriber.get()
                await websocket.send_json(event)

        async def input_loop() -> None:
            assert session is not None
            while True:
                payload = await websocket.receive_text()
                try:
                    message = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                message_type = message.get("type")
                if message_type == "input":
                    data = str(message.get("data", ""))
                    if data:
                        session.write(data)
                elif message_type == "resize":
                    session.resize(int(message.get("rows", 24) or 24), int(message.get("columns", 80) or 80))

        # Three racers, not two. The input loop ends when the client goes away and the output
        # loop when the session does, but neither ends because the *daemon* was asked to stop —
        # and a websocket the daemon cannot end holds its shutdown open exactly as a stream
        # does. The same signal the streams race, for the same reason.
        tasks = [
            asyncio.create_task(output_loop()),
            asyncio.create_task(input_loop()),
            asyncio.create_task(state.shutting_down.wait()),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for pending_task in pending:
            pending_task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for done_task in done:
            done_task.result()  # re-raise whatever ended the connection, including a clean stop
    except WebSocketDisconnect:
        pass
    except ValueError as exception:
        with suppress(Exception):
            await websocket.send_json({
                "type": "error",
                "code": "terminal_directory_invalid",
                "message": str(exception),
                "recoverable": False,
            })
    except Exception as exception:
        # Logged, and that is not decoration. Everything this handler knows about a failure used
        # to leave through the socket it was failing on: if the send did not land — and when the
        # connection is what broke, it does not — the daemon's log said nothing at all, and a
        # terminal that reconnected forever looked from the outside like a client bug.
        logger.exception("terminal websocket failed (key=%s, directory=%s)", terminal_key, working_directory)
        with suppress(Exception):
            await websocket.send_json({
                "type": "error",
                "code": "terminal_connection_failed",
                "message": str(exception),
                "recoverable": True,
            })
    finally:
        if session is not None and subscriber is not None:
            session.unsubscribe(subscriber)
