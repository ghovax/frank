"""The loop that fires schedules: one task, waking each minute, starting whatever is due."""

from __future__ import annotations

import asyncio
import logging

from langmesh.commons.services import schedules as schedule_service
from langmesh.base.errors import log_fields

logger = logging.getLogger(__name__)

#: How often the loop looks, matched to cron's own resolution of a minute.
TICK_SECONDS = 30


async def _fire(record) -> None:
    """Run one schedule: mint a session, send it the prompt, write down what happened."""
    from langmesh.daemon import api

    session_id = ""
    try:
        created = await api._session_create(
            {
                "agent": record.agent,
                "working_directory": record.working_directory,
                # Stated by the schedule, never inherited from the workspace — see ScheduleRecord.
                "permission_mode": record.permission_mode,
                "workspace_id": record.workspace_id,
                "title": record.name,
            }
        )
        session_id = str(created.get("id") or "")
        # The same shape `langmesh send` uses, so a scheduled turn and a typed one arrive the same way.
        await api._session_send(
            {
                "id": session_id,
                "parts": [{"kind": "text", "text": record.prompt}],
            }
        )
    except Exception as error:  # noqa: BLE001 — one bad schedule must not stop the rest
        logger.warning(
            "schedule could not run",
            extra=log_fields(error, schedule=record.id, schedule_name=record.name),
        )
        await asyncio.to_thread(
            schedule_service.record_run, record.id, session_id=session_id, error=str(error)
        )
        return
    logger.info("schedule %s (%r) started session %s", record.id, record.name, session_id)
    await asyncio.to_thread(schedule_service.record_run, record.id, session_id=session_id)


async def run() -> None:
    """Wake, fire what is due, sleep, catching every failure so one bad row cannot take the loop down."""
    logger.info("scheduler: watching for due schedules every %ds", TICK_SECONDS)
    while True:
        try:
            due = await asyncio.to_thread(schedule_service.due_now)
            for record in due:
                await _fire(record)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("scheduler: tick failed")
        await asyncio.sleep(TICK_SECONDS)
