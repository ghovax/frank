"""How the interface should look and where it should open, kept where everything else is kept."""

from __future__ import annotations

from typing import Any, Optional

from langmesh.base.sqlite_lock import sqlite_write_lock
from langmesh.commons import state
from langmesh.commons.database import SOLE_INTERFACE, InterfacePreferenceRecord

# What a fresh install answers with, stated once so the route and the columns cannot disagree.
DEFAULTS: dict[str, Any] = {
    "color_mode": "system",
    "locale": "",
    "last_workspace_id": "",
    "computer_control_awaiting_grant": False,
}

# The colour modes the interface can be in; anything else is refused rather than stored.
COLOR_MODES = ("system", "light", "dark")


def _as_payload(record: Optional[InterfacePreferenceRecord]) -> dict[str, Any]:
    if record is None:
        return dict(DEFAULTS)
    return {
        "color_mode": record.color_mode or DEFAULTS["color_mode"],
        "locale": record.locale or DEFAULTS["locale"],
        "last_workspace_id": record.last_workspace_id or DEFAULTS["last_workspace_id"],
        "computer_control_awaiting_grant": bool(record.computer_control_awaiting_grant),
    }


def preferences_payload() -> dict[str, Any]:
    """Everything the interface remembers, as one object, because it wants all of it at once."""
    assert state.session_factory is not None
    database = state.session_factory()
    try:
        return _as_payload(database.get(InterfacePreferenceRecord, SOLE_INTERFACE))
    finally:
        database.close()


def update_preferences(changes: dict[str, Any]) -> dict[str, Any]:
    """Apply a partial update and answer with the whole of what is now stored."""
    known = {name: value for name, value in changes.items() if name in DEFAULTS and value is not None}
    if "color_mode" in known and known["color_mode"] not in COLOR_MODES:
        raise ValueError(f"Unknown colour mode {known['color_mode']!r}.")
    assert state.session_factory is not None
    with sqlite_write_lock():
        database = state.session_factory()
        try:
            record = database.get(InterfacePreferenceRecord, SOLE_INTERFACE)
            if record is None:
                record = InterfacePreferenceRecord(id=SOLE_INTERFACE, **{**DEFAULTS, **known})
                database.add(record)
            else:
                for name, value in known.items():
                    setattr(record, name, value)
            database.commit()
            return _as_payload(record)
        finally:
            database.close()
