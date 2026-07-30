"""Schedule domain: recurring prompts, and working out when one is next due.

A schedule is a prompt, a workspace, an agent and a cron line. Firing one creates an ordinary
session and sends it that prompt — there is no separate unattended execution path, because a
second way of running a turn is a second thing to keep correct.

What *is* different is that nobody is watching, and everything unusual here follows from that:
the permission mode is stated rather than inherited, the timezone is stored rather than assumed,
and a missed window is caught up exactly once rather than replayed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone as _utc
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from frank.base.sqlite_lock import sqlite_write_lock
from frank.hub import state
from frank.hub.database import ScheduleRecord, WorkspaceRecord

#: The modes a schedule may run under. Deliberately the same three a person may choose, minus
#: nothing: a scheduled job is not a lesser kind of session and may legitimately need to write.
PERMISSION_MODES = ("default", "auto", "read_only")


class ScheduleError(ValueError):
    """A schedule that cannot be created or run, with a sentence saying why."""


def _now() -> str:
    return datetime.now(_utc.utc).isoformat()


def validate(cron: str, zone: str, permission_mode: str) -> None:
    """Reject a schedule that could never fire correctly, at the moment it is written.

    All three are checked here rather than at the first firing, because the first firing may be
    days away and at three in the morning. A cron line that does not parse, a timezone the host
    has never heard of, and a permission mode nobody chose are each a mistake somebody can fix
    now and cannot fix then."""
    if not croniter.is_valid(cron):
        raise ScheduleError(f"{cron!r} is not a cron expression.")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ScheduleError(f"{zone!r} is not a timezone this machine knows.") from error
    if permission_mode not in PERMISSION_MODES:
        # Never defaulted. A schedule runs with nobody present, so the mode is the one thing
        # its author must decide rather than discover — inheriting the workspace's would mean a
        # job silently gaining write access the day somebody loosened the workspace.
        raise ScheduleError(
            "A schedule must state its permission mode explicitly (one of: "
            + ", ".join(PERMISSION_MODES) + "), because it runs with nobody watching."
        )


def next_firing(record: ScheduleRecord, after: Optional[datetime] = None) -> datetime:
    """When this schedule is next due, in UTC."""
    zone = ZoneInfo(record.timezone)
    moment = (after or datetime.now(_utc.utc)).astimezone(zone)
    return croniter(record.cron, moment).get_next(datetime).astimezone(_utc.utc)


def is_due(record: ScheduleRecord, *, now: Optional[datetime] = None) -> bool:
    """Whether this schedule should fire on this tick.

    Measured from the last firing rather than from the clock, so a daemon that was asleep over
    a window still runs the job once when it wakes — and *once* is the point. Asking "is the
    current minute a match" instead would drop the run entirely on a machine that was closed;
    replaying every window since would open a hundred sessions on a laptop back from a holiday.
    """
    if not record.enabled:
        return False
    moment = now or datetime.now(_utc.utc)
    if not record.last_fired_at:
        # Never fired: due from the moment it was created, not from the epoch.
        anchor = datetime.fromisoformat(record.created_at)
    else:
        anchor = datetime.fromisoformat(record.last_fired_at)
    return next_firing(record, anchor) <= moment


def serialize(record: ScheduleRecord) -> dict[str, Any]:
    """One schedule as a caller reads it, with the next firing worked out rather than stored.

    Derived on read because a stored "next run" is a fact with a shelf life: it goes stale the
    moment the cron line or the timezone is edited, and nothing would be obviously wrong."""
    try:
        upcoming = next_firing(record).isoformat()
    except Exception:  # noqa: BLE001 — a bad cron line must not make the listing unreadable
        upcoming = ""
    return {
        "id": record.id, "workspace_id": record.workspace_id, "name": record.name,
        "cron": record.cron, "timezone": record.timezone, "agent": record.agent,
        "prompt": record.prompt, "permission_mode": record.permission_mode,
        "working_directory": record.working_directory, "enabled": bool(record.enabled),
        "last_fired_at": record.last_fired_at, "last_session_id": record.last_session_id,
        "last_error": record.last_error, "created_at": record.created_at,
        "next_firing": upcoming,
    }


def _database():
    assert state.session_factory is not None
    return state.session_factory()


def create(*, workspace_id: str, name: str, cron: str, prompt: str, agent: str,
           permission_mode: str, timezone_name: str, working_directory: str = "") -> dict[str, Any]:
    validate(cron, timezone_name, permission_mode)
    if not name.strip():
        raise ScheduleError("A schedule needs a name — it is how you will recognise it later.")
    if not prompt.strip():
        raise ScheduleError("A schedule needs a prompt; there is nothing to run without one.")
    database_session = _database()
    try:
        if not database_session.get(WorkspaceRecord, workspace_id):
            raise ScheduleError(f"No workspace {workspace_id!r}.")
        record = ScheduleRecord(
            id=str(uuid.uuid4()), workspace_id=workspace_id, name=name.strip(), cron=cron,
            timezone=timezone_name, agent=agent, prompt=prompt, permission_mode=permission_mode,
            working_directory=working_directory, enabled=True, last_fired_at="",
            last_session_id="", last_error="", created_at=_now(), updated_at=_now(),
        )
        with sqlite_write_lock():
            database_session.add(record)
            database_session.commit()
        payload = serialize(record)
    finally:
        database_session.close()
    _announce()
    return payload


def listing(workspace_id: str = "") -> list[dict[str, Any]]:
    database_session = _database()
    try:
        query = database_session.query(ScheduleRecord)
        if workspace_id:
            query = query.filter(ScheduleRecord.workspace_id == workspace_id)
        return [serialize(row) for row in query.order_by(ScheduleRecord.created_at.desc()).all()]
    finally:
        database_session.close()


def get(schedule_id: str) -> dict[str, Any]:
    database_session = _database()
    try:
        record = database_session.get(ScheduleRecord, schedule_id)
        if record is None:
            raise ScheduleError(f"No schedule {schedule_id!r}.")
        return serialize(record)
    finally:
        database_session.close()


def set_enabled(schedule_id: str, enabled: bool) -> dict[str, Any]:
    database_session = _database()
    try:
        record = database_session.get(ScheduleRecord, schedule_id)
        if record is None:
            raise ScheduleError(f"No schedule {schedule_id!r}.")
        with sqlite_write_lock():
            record.enabled = enabled
            record.updated_at = _now()
            database_session.commit()
        payload = serialize(record)
    finally:
        database_session.close()
    _announce()
    return payload


def delete(schedule_id: str) -> None:
    database_session = _database()
    try:
        record = database_session.get(ScheduleRecord, schedule_id)
        if record is None:
            raise ScheduleError(f"No schedule {schedule_id!r}.")
        with sqlite_write_lock():
            database_session.delete(record)
            database_session.commit()
    finally:
        database_session.close()
    _announce()


def record_run(schedule_id: str, *, session_id: str = "", error: str = "") -> None:
    """Write down what a firing produced — the session it started, or why it could not.

    The timestamp moves whether the run succeeded or failed, deliberately: a schedule whose
    agent has been deleted would otherwise be retried every single tick, and the loop would
    spend the night failing instead of waiting for the next window."""
    database_session = _database()
    try:
        record = database_session.get(ScheduleRecord, schedule_id)
        if record is None:
            return
        with sqlite_write_lock():
            record.last_fired_at = _now()
            record.last_session_id = session_id
            record.last_error = error
            record.updated_at = _now()
            database_session.commit()
    finally:
        database_session.close()
    _announce()


def due_now(*, now: Optional[datetime] = None) -> list[ScheduleRecord]:
    """Every enabled schedule whose window has passed. Detached copies, so the caller can act
    on them without holding a database session open across the turns it starts."""
    database_session = _database()
    try:
        rows = database_session.query(ScheduleRecord).filter(ScheduleRecord.enabled.is_(True)).all()
        due = []
        for row in rows:
            try:
                if is_due(row, now=now):
                    due.append(row)
            except Exception:  # noqa: BLE001 — one unparseable row must not stop the others
                continue
        for row in due:
            database_session.expunge(row)
        return due
    finally:
        database_session.close()


def _announce() -> None:
    if state.broadcaster is not None:
        state.broadcaster.publish({"type": "schedules_changed"})
