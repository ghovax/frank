"""Schedules as *rows*: creating them, listing them, and recording what a firing did.

A schedule is a prompt, a workspace, an agent and a cron line. Firing one creates an ordinary
session and sends it that prompt — there is no separate unattended execution path, because a
second way of running a turn is a second thing to keep correct.

What *is* different is that nobody is watching, and everything unusual here follows from that:
the permission mode is stated rather than inherited, the timezone is stored rather than assumed,
and a missed window is caught up exactly once rather than replayed.

What a cron line *means* is not here. It is in `frank.base.schedules`, in values, because
"is `0 9 * * MON-FRI` in `Europe/Rome` due yet" is a question about three strings and has no
opinion about SQLAlchemy — and while it lived here, every caller that wanted to ask it had to
import a database first. This module is the half that genuinely needs one: the durable row, the
transaction, and the facts a firing writes down for the next tick to read.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone as _utc
from typing import Any, Optional

from frank.base.schedules import (
    PERMISSION_MODES,
    ScheduleError,
    is_due,
    next_firing,
    validate,
)
from frank.base.sqlite_lock import sqlite_write_lock
from frank.hub import state
from frank.hub.database import ScheduleRecord, WorkspaceRecord

# `ScheduleError` and `PERMISSION_MODES` are re-exported rather than re-imported at each call site: the daemon's API and the REST routes catch one error for one concept, and which module happens to define it is not a distinction worth pushing onto them.
__all__ = ["PERMISSION_MODES", "ScheduleError", "create", "delete", "due_now", "get",
           "listing", "next_firing", "record_run", "serialize", "set_enabled", "validate"]


def _now() -> str:
    return datetime.now(_utc.utc).isoformat()


def _record_is_due(record: ScheduleRecord, *, now: Optional[datetime] = None) -> bool:
    """Whether this stored schedule should fire on this tick.

    The anchor is the last firing, or the moment it was created when it has never fired — which
    is what makes a daemon that was asleep over a window run the job once on waking rather than
    dropping it or replaying every window since."""
    if not record.enabled:
        return False
    anchor = datetime.fromisoformat(record.last_fired_at or record.created_at)
    return is_due(record.cron, record.timezone, since=anchor, now=now)


def serialize(record: ScheduleRecord) -> dict[str, Any]:
    """One schedule as a caller reads it, with the next firing worked out rather than stored.

    Derived on read because a stored "next run" is a fact with a shelf life: it goes stale the
    moment the cron line or the timezone is edited, and nothing would be obviously wrong."""
    try:
        upcoming = next_firing(record.cron, record.timezone).isoformat()
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
                if _record_is_due(row, now=now):
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
