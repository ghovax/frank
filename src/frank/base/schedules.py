"""What a recurring schedule *means*: when it fires, and whether it could ever fire correctly.

Separated from the store that keeps schedules, and that separation is the point. A schedule as a
feature needs three things the library deliberately does not have — a durable row that outlives
the process, a loop that wakes to ask what is due, and a registry to mint the session a firing
creates. Those belong to the daemon, and that is where they stay.

None of this does. Whether `0 9 * * MON-FRI` in `Europe/Rome` is due at a given instant is a
question about a cron line, a timezone and a timestamp; it has no opinion about SQLAlchemy. It
lived in the hub's service anyway, taking a `ScheduleRecord`, so every caller that wanted to
answer it — the daemon's loop, the REST layer, the interface's preview of the next few firings —
had to import a database to ask.

So the semantics live here, in values, and the store above composes with them. A program using
`frank` as a library can now schedule its own work: it gets the meaning of a schedule and
supplies its own store and its own loop, which is exactly the split the layers describe.

The one behaviour worth reading before changing: due-ness is measured from the **last firing**,
not from the current minute. Asking "is this minute a match" drops a run entirely on a machine
that was asleep over the window; replaying every window since would open a hundred sessions on a
laptop back from a holiday. Measuring from the last firing gives exactly one catch-up run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from frank.base.permission_mode import PermissionMode

__all__ = [
    "PERMISSION_MODES",
    "ScheduleError",
    "is_due",
    "next_firing",
    "validate",
]

#: The modes a schedule may run under. Deliberately every mode a person may choose, minus
#: nothing: a scheduled job is not a lesser kind of session and may legitimately need to write.
#: Read off the enum rather than restated, so a mode added there is offered here without an edit
#: — this list was a hand-kept copy, which is the kind that falls behind silently.
PERMISSION_MODES = tuple(str(mode) for mode in PermissionMode)


class ScheduleError(ValueError):
    """A schedule that cannot be created or run, with a sentence saying why."""


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


def next_firing(cron: str, zone: str, after: Optional[datetime] = None) -> datetime:
    """When a cron line next comes due, in UTC.

    Evaluated in `zone` and returned in UTC, because those are different jobs: "nine every
    weekday" means nine *where the person is*, while a comparison against other timestamps wants
    one clock. A machine that moves, or that observes daylight saving, would otherwise drift by
    an hour twice a year with nothing looking wrong."""
    where = ZoneInfo(zone)
    moment = (after or datetime.now(timezone.utc)).astimezone(where)
    return croniter(cron, moment).get_next(datetime).astimezone(timezone.utc)


def is_due(cron: str, zone: str, *, since: datetime, now: Optional[datetime] = None) -> bool:
    """Whether a schedule last fired at `since` should fire now.

    `since` is the last firing, or — for a schedule that has never fired — the moment it was
    created, so a new schedule is due from when it was written rather than from the epoch."""
    moment = now or datetime.now(timezone.utc)
    return next_firing(cron, zone, since) <= moment
