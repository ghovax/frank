"""Projects routes."""

from __future__ import annotations
from fastapi import APIRouter
from fastapi import HTTPException
from daisy.locations.executor import SshExecutor
from daisy.locations.resolver import host_is_defined
from pathlib import Path
import asyncio
from daisy.protocol.dtos import (
    LocationInput,
    ProjectCreateRequest,
)
from daisy.daemon.services import projects as _projects
from daisy.daemon.services.broadcast import _publish_broadcast
from daisy.daemon.services.projects import _create_location, _create_project, _delete_location, _delete_project, _hosts_payload, _project_name, _project_payload, _projects_payload, _update_location

router = APIRouter()

@router.get("/home")
async def home_directory():
    """The daemon user's home directory and its folder name — the default project
    the UI selects before anything else is chosen."""
    home = str(Path.home())
    return {"path": home, "name": _project_name(home)}


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


@router.get("/projects")
async def list_projects():
    return await asyncio.to_thread(_projects_payload)


@router.post("/projects")
async def create_project(request: ProjectCreateRequest):
    try:
        project = await asyncio.to_thread(_create_project, request)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    _publish_broadcast({"type": "projects_changed"})
    return project


@router.get("/projects/{project_id}")
async def get_project(project_id: str):
    project = await asyncio.to_thread(_project_payload, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return project


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    # There is always exactly one active project in the UI, so the last one can't be
    # deleted — that would leave an empty state the redesigned app no longer has.
    if await asyncio.to_thread(_projects._project_count) <= 1:
        raise HTTPException(status_code=400, detail="Can't delete the only project.")
    deleted = await asyncio.to_thread(_delete_project, project_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Project not found.")
    _publish_broadcast({"type": "projects_changed"})
    _publish_broadcast({"type": "sessions_changed"})
    return {"ok": True}


@router.post("/projects/{project_id}/locations")
async def create_location(project_id: str, request: LocationInput):
    try:
        location = await asyncio.to_thread(_create_location, project_id, request)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if location is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    _publish_broadcast({"type": "projects_changed"})
    return location


@router.put("/locations/{location_id}")
async def update_location(location_id: str, request: LocationInput):
    try:
        location = await asyncio.to_thread(_update_location, location_id, request)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if location is None:
        raise HTTPException(status_code=404, detail="Location not found.")
    _publish_broadcast({"type": "projects_changed"})
    return location


@router.delete("/locations/{location_id}")
async def delete_location(location_id: str):
    deleted = await asyncio.to_thread(_delete_location, location_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Location not found.")
    _publish_broadcast({"type": "projects_changed"})
    return {"ok": True}
