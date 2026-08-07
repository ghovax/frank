"""Workspace routes."""

from __future__ import annotations
from fastapi import APIRouter, HTTPException
from langmesh.locations.executor import SshExecutor
from langmesh.locations.resolver import host_is_defined
from pathlib import Path
import asyncio
from langmesh.protocol.dtos import (
    LocationInput,
    WorkspaceCreateRequest,
    WorkspaceLastSessionRequest,
)
from langmesh.commons import state
from langmesh.commons.services import workspaces as _workspaces
from langmesh.commons.services.broadcast import _publish_broadcast
from langmesh.commons.services.locations import _workspace_id_for_location
from langmesh.commons.services.workspaces import (
    _create_location,
    _create_workspace,
    _delete_location,
    _delete_workspace,
    _hosts_payload,
    _remember_last_session,
    _workspace_name,
    _workspace_payload,
    _workspaces_payload,
    _update_location,
)

router = APIRouter()


@router.get("/home")
async def home_directory():
    """The daemon user's home directory and its folder name: the workspace the UI selects by default."""
    home = str(Path.home())
    return {"path": home, "name": _workspace_name(home)}


@router.get("/hosts")
async def list_hosts():
    """The connectable SSH hosts from ~/.ssh/config (the source of truth for remotes)."""
    return await asyncio.to_thread(_hosts_payload)


@router.get("/hosts/{alias}/home")
async def host_home_directory(alias: str):
    """Best-effort home directory of a host, for prefilling a base directory, empty when it cannot be reached."""

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
    # There is always one active workspace, so deleting the last would leave a state the app has not got.
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
    """Remember where a workspace was last opened, in the daemon, since every client shares that memory."""
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
    # The sessions already open are told too, or a new environment exists in Settings and nowhere else.
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
    # Read the workspace off the row first: after the delete there is nothing left to ask.
    workspace_id = await asyncio.to_thread(_workspace_id_for_location, location_id)
    deleted = await asyncio.to_thread(_delete_location, location_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Environment not found.")
    _publish_broadcast({"type": "workspaces_changed"})
    await state.workspace_locations_changed(workspace_id)
    return {"ok": True}
