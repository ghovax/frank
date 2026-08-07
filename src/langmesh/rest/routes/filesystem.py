"""Filesystem routes."""

from __future__ import annotations
from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse
from typing import cast
from watchfiles import awatch
import asyncio
import subprocess
from contextlib import suppress
from langmesh.protocol.dtos import (
    DirectoryRevealRequest,
    DirectoryValidationRequest,
)
from langmesh.commons import state
from langmesh.rest.services.filesystem import (
    _GIT_STATUS_WATCH_FILTER,
    _git_status_changes_relevant,
    _git_status_key,
    _git_status_watch_paths,
    _open_folder_picker,
    _validate_directory_payload,
)
from langmesh.base.serialization import compact

router = APIRouter()


async def _idle(seconds: float) -> bool:
    """Sleep, unless the daemon is stopping. Answers whether it is."""
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(state.shutting_down.wait(), timeout=seconds)
    return state.shutting_down.is_set()


@router.get("/messages/history")
async def get_message_history(working_directory: str = ""):
    """Return the last 100 user messages sent in this project, newest first."""
    if not working_directory:
        return {"messages": []}
    assert state.turn_store is not None
    messages = await state.turn_store.get_user_messages(working_directory)
    return {"messages": messages}


@router.post("/messages/history")
async def save_message_history(body: dict):
    """Persist a user message for up/down arrow recall within the project."""
    assert state.turn_store is not None
    await state.turn_store.add_user_message(body["working_directory"], body["message"])
    return {"ok": True}


@router.post("/directory/validate")
async def validate_directory(request: DirectoryValidationRequest):
    """Validate that a path is an existing absolute directory and report Git workspace availability."""
    # Off the loop: a git probe can block for seconds and would freeze every other request.
    return await asyncio.to_thread(_validate_directory_payload, request.directory.strip())


@router.get("/git/status/stream")
async def git_status_stream(directory: str, request: Request):
    """Stream git status changes for the selected directory."""
    directory = directory.strip()

    async def event_generator():
        payload = await asyncio.to_thread(_validate_directory_payload, directory)
        previous_key = _git_status_key(payload)
        yield {"event": "message", "data": compact(payload)}

        watch_paths = _git_status_watch_paths(directory, payload)
        if not watch_paths:
            # Waits on the client going away or the daemon going down, whichever comes first.
            while not await request.is_disconnected():
                if await _idle(30):
                    return
            return

        try:
            # The stop event is why this can be shut down at all, since the watch is parked waiting for a change.
            async for changes in awatch(
                *watch_paths,
                watch_filter=_GIT_STATUS_WATCH_FILTER,
                debounce=500,
                stop_event=state.shutting_down,
            ):
                if state.shutting_down.is_set() or await request.is_disconnected():
                    break
                typed_changes = cast(set[tuple[object, str]], changes)
                if not await asyncio.to_thread(
                    _git_status_changes_relevant, directory, payload, typed_changes
                ):
                    continue
                payload = await asyncio.to_thread(_validate_directory_payload, directory)
                next_key = _git_status_key(payload)
                if next_key == previous_key:
                    continue
                previous_key = next_key
                yield {"event": "message", "data": compact(payload)}
        except asyncio.CancelledError:
            pass

    return EventSourceResponse(event_generator(), ping=15)


@router.post("/directory/reveal")
async def reveal_directory(request: DirectoryRevealRequest):
    """Reveal a directory in the native file manager (macOS Finder, other OS file manager)."""
    path = request.path.strip()
    if not path:
        raise HTTPException(status_code=400, detail="Path is required.")
    try:
        subprocess.Popen(["open", "-R", path])
        return {"revealed": True}
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


# The macOS application name to open a browser's own remote-debugging settings page in.
_BROWSER_APP_NAMES = {"chrome": "Google Chrome", "edge": "Microsoft Edge", "brave": "Brave Browser"}


@router.post("/browser/enable-remote-debugging")
async def open_browser_remote_debugging(browser_name: str = "chrome"):
    """Open the browser's remote-debugging settings page so the user can turn the switch on."""
    from langmesh.computer.web import REMOTE_DEBUGGING_URL

    app_name = _BROWSER_APP_NAMES.get(browser_name, _BROWSER_APP_NAMES["chrome"])
    try:
        subprocess.Popen(["open", "-a", app_name, REMOTE_DEBUGGING_URL])
        return {"opened": True}
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))


@router.post("/directory/browse")
async def browse_directory():
    """Open a native folder picker on the local server machine and return an absolute path."""
    # The native picker blocks until the user chooses, so it must run off the event loop.
    return await asyncio.to_thread(_open_folder_picker)


@router.get("/filesystem/leases")
async def filesystem_leases():
    """Active filesystem mutation leases across all sessions in this backend."""
    return {
        "leases": state.file_lease_manager.active() if state.file_lease_manager is not None else []
    }
