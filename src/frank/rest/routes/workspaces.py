"""Workspace routes."""

from __future__ import annotations
from fastapi import APIRouter, HTTPException
from frank.locations.executor import SshExecutor
from frank.locations.resolver import host_is_defined
from pathlib import Path
import asyncio
from frank.protocol.dtos import (
    LocationInput,
    WorkspaceCreateRequest,
    WorkspaceLastSessionRequest,
)
from frank.hub import state
from frank.hub.services import workspaces as _workspaces
from frank.hub.services.broadcast import _publish_broadcast
from frank.hub.services.locations import _workspace_id_for_location
from frank.hub.services.workspaces import _create_location, _create_workspace, _delete_location, _delete_workspace, _hosts_payload, _remember_last_session, _workspace_name, _workspace_payload, _workspaces_payload, _update_location

router = APIRouter()

@router.get("/home")
async def home_directory():
    """The daemon user's home directory and its folder name — the default workspace
    the UI selects before anything else is chosen."""
    home = str(Path.home())
    return {"path": home, "name": _workspace_name(home)}


@router.get("/hosts")
async def list_hosts():
    """The connectable SSH hosts from ~/.ssh/config (the source of truth for remotes)."""
    return await asyncio.to_thread(_hosts_payload)


@router.get("/hosts/{alias}/home")
async def host_home_directory(alias: str):
    """Best-effort resolution of a host's home directory, for prefilling a new location's
    base directory with an editable starter. Runs `printf $HOME` over SSH with a short
    timeout; returns an empty path if the host is unknown, unreachable, or not yet
    authenticated (the field then just stays empty for manual entry)."""
    def _resolve() -> str:
        if not host_is_defined(alias):
            return ""
        try:
            result = SshExecutor(alias).run('printf %s "$HOME"', ".", timeout=8.0)
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""
    return {"alias": alias, "path": await asyncio.to_thread(_resolve)}


@router.get("/workspaces")
async def list_workspaces():
    return await asyncio.to_thread(_workspaces_payload)


@router.post("/workspaces")
async def create_project(request: WorkspaceCreateRequest):
    try:
        workspace = await asyncio.to_thread(_create_workspace, request)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    _publish_broadcast({"type": "workspaces_changed"})
    return workspace


@router.get("/workspaces/{workspace_id}")
async def get_project(workspace_id: str):
    workspace = await asyncio.to_thread(_workspace_payload, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return workspace


@router.delete("/workspaces/{workspace_id}")
async def delete_project(workspace_id: str):
    # There is always exactly one active workspace in the UI, so the last one can't be
    # deleted — that would leave an empty state the redesigned app no longer has.
    if await asyncio.to_thread(_workspaces._workspace_count) <= 1:
        raise HTTPException(status_code=400, detail="Can't delete the only workspace.")
    deleted = await asyncio.to_thread(_delete_workspace, workspace_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Project not found.")
    _publish_broadcast({"type": "workspaces_changed"})
    _publish_broadcast({"type": "sessions_changed"})
    return {"ok": True}


@router.put("/workspaces/{workspace_id}/last-session")
async def remember_last_session(workspace_id: str, request: WorkspaceLastSessionRequest):
    """Remember which conversation this workspace was last opened at, so the next client to open
    it lands where the person left off.

    This is deliberately the daemon's memory rather than the browser's. The desktop app, a
    browser, and the phone are three windows onto one machine; "the conversation I was in" is a
    fact about that machine, and storing it per-client means each one reopens somewhere
    different — and the phone, whose storage is cleared whenever the webview is, reopens nowhere
    at all.

    No broadcast: this only decides where a *later* launch lands, and telling every open client
    to re-read the workspace list because one of them changed conversations would be a lot of
    traffic to no visible end."""
    if not await asyncio.to_thread(_remember_last_session, workspace_id, request.session_id):
        raise HTTPException(status_code=404, detail="Project not found.")
    return {"ok": True}


@router.post("/workspaces/{workspace_id}/locations")
async def create_location(workspace_id: str, request: LocationInput):
    try:
        location = await asyncio.to_thread(_create_location, workspace_id, request)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if location is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    _publish_broadcast({"type": "workspaces_changed"})
    # The sessions already open in this workspace are told too. Without it the new
    # environment existed in Settings and in no conversation — every session that
    # predated the edit went on addressing the set it was created with.
    await state.workspace_locations_changed(workspace_id)
    return location


@router.put("/locations/{location_id}")
async def update_location(location_id: str, request: LocationInput):
    try:
        location = await asyncio.to_thread(_update_location, location_id, request)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if location is None:
        raise HTTPException(status_code=404, detail="Environment not found.")
    _publish_broadcast({"type": "workspaces_changed"})
    await state.workspace_locations_changed(str(location.get("workspace_id") or ""))
    return location


@router.delete("/locations/{location_id}")
async def delete_location(location_id: str):
    # Read the workspace off the row before it goes: after the delete there is nothing left
    # to ask which workspace's sessions need telling.
    workspace_id = await asyncio.to_thread(_workspace_id_for_location, location_id)
    deleted = await asyncio.to_thread(_delete_location, location_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Environment not found.")
    _publish_broadcast({"type": "workspaces_changed"})
    await state.workspace_locations_changed(workspace_id)
    return {"ok": True}
