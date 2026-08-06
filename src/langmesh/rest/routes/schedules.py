"""Schedule routes: recurring prompts, and firing one by hand."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from langmesh.commons.services import schedules as _schedules

router = APIRouter()


class ScheduleCreateRequest(BaseModel):
    workspace_id: str
    name: str
    cron: str
    prompt: str
    agent: str
    # No default: a schedule runs unwatched, so its author decides the mode rather than discovering it.
    permission_mode: str
    timezone: str
    working_directory: str = Field(default="")


class ScheduleEnabledRequest(BaseModel):
    enabled: bool


def _fail(error: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(error))


@router.get("/schedules")
async def list_schedules(workspace_id: str = ""):
    # Dispatched off the loop, since the write lock refuses to run on it rather than deadlocking quietly.
    return {"schedules": await asyncio.to_thread(_schedules.listing, workspace_id)}


@router.post("/schedules")
async def create_schedule(request: ScheduleCreateRequest):
    try:
        return await asyncio.to_thread(
            _schedules.create,
            workspace_id=request.workspace_id, name=request.name, cron=request.cron,
            prompt=request.prompt, agent=request.agent,
            permission_mode=request.permission_mode, timezone_name=request.timezone,
            working_directory=request.working_directory,
        )
    except _schedules.ScheduleError as error:
        raise _fail(error) from None


@router.get("/schedules/{schedule_id}")
async def read_schedule(schedule_id: str):
    try:
        return await asyncio.to_thread(_schedules.get, schedule_id)
    except _schedules.ScheduleError as error:
        raise HTTPException(status_code=404, detail=str(error)) from None


@router.patch("/schedules/{schedule_id}")
async def set_schedule_enabled(schedule_id: str, request: ScheduleEnabledRequest):
    try:
        return await asyncio.to_thread(_schedules.set_enabled, schedule_id, request.enabled)
    except _schedules.ScheduleError as error:
        raise HTTPException(status_code=404, detail=str(error)) from None


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str):
    try:
        await asyncio.to_thread(_schedules.delete, schedule_id)
    except _schedules.ScheduleError as error:
        raise HTTPException(status_code=404, detail=str(error)) from None
    return {"deleted": schedule_id}


@router.post("/schedules/{schedule_id}/run")
async def run_schedule(schedule_id: str):
    """Fire now without moving the window, so a wrong agent name is found before six tomorrow morning."""
    from langmesh.daemon import scheduler
    from langmesh.commons.database import ScheduleRecord
    from langmesh.commons import state as commons_state

    def _detached():
        database_session = commons_state.session_factory()
        try:
            found = database_session.get(ScheduleRecord, schedule_id)
            if found is not None:
                database_session.expunge(found)
            return found
        finally:
            database_session.close()

    record = await asyncio.to_thread(_detached)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No schedule {schedule_id!r}.")
    await scheduler._fire(record)
    return await asyncio.to_thread(_schedules.get, schedule_id)
