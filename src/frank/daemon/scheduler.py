"""The loop that fires schedules: one task, waking each minute, starting whatever is due.

One loop rather than a timer per schedule. A timer per schedule is more precise and buys
nothing here — a job that runs at 09:00:00 rather than 09:00:37 is not a better job — while
costing a set of tasks to cancel and rebuild every time a schedule is created, edited, paused
or deleted, and a way for those tasks to disagree with the table. The loop reads the table.

Firing creates an ordinary session and sends it the prompt, through the same calls a person's
client makes. There is deliberately no unattended execution path: a second way to run a turn is
a second thing to keep correct, and the one that runs unwatched is the one nobody would notice
had drifted.
"""

from __future__ import annotations

import asyncio
import logging

from frank.hub.services import schedules as schedule_service
from frank.base.errors import describe
from frank.base.serialization import compact

logger = logging.getLogger(__name__)

#: How often the loop looks.
TICK_SECONDS = 30


async def _fire(record) -> None:
    """Run one schedule: mint a session, send it the prompt, write down what happened."""
    from frank.daemon import api

    session_id = ""
    try:
        created = await api._session_create({
            "agent": record.agent,
            "working_directory": record.working_directory,
            # Stated by the schedule, never inherited from the workspace — see ScheduleRecord.
            "permission_mode": record.permission_mode,
            "workspace_id": record.workspace_id,
            "title": record.name,
        })
        session_id = str(created.get("id") or "")
        # The same shape `frank send` uses — one wire format, so a scheduled turn and a typed one are the same thing arriving by different doors.
        await api._session_send({
            "id": session_id,
            "parts": [{"kind": "text", "text": record.prompt}],
        })
    except Exception as error:  # noqa: BLE001 — one bad schedule must not stop the rest
        logger.warning("schedule could not run %s", compact({
                "schedule": record.id, "name": record.name, **describe(error),
            }))
        await asyncio.to_thread(
            schedule_service.record_run, record.id, session_id=session_id, error=str(error))
        return
    logger.info("schedule %s (%r) started session %s", record.id, record.name, session_id)
    await asyncio.to_thread(schedule_service.record_run, record.id, session_id=session_id)


async def run() -> None:
    """Wake, fire what is due, sleep. Forever, until the daemon cancels it.

    Every failure is caught and logged rather than raised. A scheduler that dies on one bad row
    takes every other schedule with it silently, and the person who would notice is the one who
    went to bed expecting a report in the morning."""
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
