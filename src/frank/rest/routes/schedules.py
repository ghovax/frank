"""Schedule routes: recurring prompts, and firing one by hand."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from frank.hub.services import schedules as _schedules

router = APIRouter()


class ScheduleCreateRequest(BaseModel):
    workspace_id: str
    name: str
    cron: str
    prompt: str
    agent: str
    # No default, and that is the point: a schedule runs with nobody watching, so the mode is the one thing its author decides rather than discovers.
    permission_mode: str
    timezone: str
    working_directory: str = Field(default="")


class ScheduleEnabledRequest(BaseModel):
    enabled: bool


def _fail(error: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(error))


@router.get("/schedules")
async def list_schedules(workspace_id: str = ""):
    # The services are synchronous by convention here, and the write lock refuses to run on the event loop rather than deadlocking quietly.
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
    """Fire now, without waiting for the window and without moving it.

    So a schedule can be tried the moment it is written rather than at six tomorrow morning,
    which is the only way to find out that the agent name was wrong before it matters."""
    from frank.daemon import scheduler
    from frank.hub.database import ScheduleRecord
    from frank.hub import state as hub_state

    def _detached():
        database_session = hub_state.session_factory()
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
