"""What a recurring schedule means: when it fires, and whether it could ever fire correctly."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from langmesh.base.permission_mode import PermissionMode

__all__ = [
    "PERMISSION_MODES",
    "ScheduleError",
    "is_due",
    "next_firing",
    "validate",
]

#: Every mode a person may choose: a scheduled job is not a lesser session and may legitimately write.
PERMISSION_MODES = tuple(str(mode) for mode in PermissionMode)


class ScheduleError(ValueError):
    """A schedule that cannot be created or run, with a sentence saying why."""


def validate(cron: str, zone: str, permission_mode: str) -> None:
    """Reject a schedule that could never fire correctly, at the moment it is written."""
    if not croniter.is_valid(cron):
        raise ScheduleError(f"{cron!r} is not a cron expression.")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ScheduleError(f"{zone!r} is not a timezone this machine knows.") from error
    if permission_mode not in PERMISSION_MODES:
        # Never defaulted: a schedule runs unwatched, so its author decides the mode rather than inherits it.
        raise ScheduleError(
            "A schedule must state its permission mode explicitly (one of: "
            + ", ".join(PERMISSION_MODES)
            + "), because it runs with nobody watching."
        )


def next_firing(cron: str, zone: str, after: Optional[datetime] = None) -> datetime:
    """When a cron line next comes due: evaluated in `zone`, returned in UTC, because those are different jobs."""
    where = ZoneInfo(zone)
    moment = (after or datetime.now(timezone.utc)).astimezone(where)
    return croniter(cron, moment).get_next(datetime).astimezone(timezone.utc)


def is_due(cron: str, zone: str, *, since: datetime, now: Optional[datetime] = None) -> bool:
    """Whether a schedule last fired at `since` should fire now, anchored on creation when it never has."""
    moment = now or datetime.now(timezone.utc)
    return next_firing(cron, zone, since) <= moment
