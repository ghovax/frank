"""Terminals routes."""
from harness.server.services.terminals import TerminalSession, _delete_terminal_state, _list_terminal_states, _terminal_context_for_request, _terminal_directory
from fastapi import APIRouter
from contextlib import suppress
from fastapi import HTTPException
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from pathlib import Path
import asyncio
import json
from harness.server import state

router = APIRouter()

@router.get("/terminals")
async def list_terminals(context_id: str = "", working_directory: str = ""):
    context_identifier = await _terminal_context_for_request(context_id, working_directory)
    persisted = await asyncio.to_thread(_list_terminal_states, context_identifier)
    live_keys = state._terminal_manager.live_keys(context_identifier) if state._terminal_manager is not None else set()
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
async def delete_terminal(terminal_key: str, context_id: str = "", working_directory: str = ""):
    terminal_key = (terminal_key or "").strip()[:128]
    if not terminal_key:
        raise HTTPException(status_code=400, detail="A terminal key is required.")
    context_identifier = await _terminal_context_for_request(context_id, working_directory)
    if state._terminal_manager is not None:
        await state._terminal_manager.close_one(context_identifier, terminal_key)
    await asyncio.to_thread(_delete_terminal_state, context_identifier, terminal_key)
    return {"ok": True}


@router.websocket("/terminal")
async def terminal_websocket(
    websocket: WebSocket,
    context_id: str = "",
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
    if state._terminal_manager is None:
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
            directory = _terminal_directory(context_id, working_directory)
        session = await state._terminal_manager.get_or_create(context_id, directory, rows, columns, terminal_key=terminal_key, remote_host_alias=remote_alias)
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

        tasks = [asyncio.create_task(output_loop()), asyncio.create_task(input_loop())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for pending_task in pending:
            pending_task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for done_task in done:
            done_task.result()
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
