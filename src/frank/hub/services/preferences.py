"""How the interface should look and where it should open, kept where everything else is kept.

The colour mode, the language, the workspace a fresh launch reopens, and the one flag a grant
flow leaves behind. Each of these used to live in the browser's `localStorage` — which made
them facts about a browser, when they are facts about this Frank. There is one daemon; a
preference is a row beside the sessions and the workspaces, and every client reads it.

One row, named columns, read and written as a whole. The set is small and fixed, so a
name/value table would only make every read a lookup and every write a place for a typo to
mean "never set".
"""

from __future__ import annotations

from typing import Any, Optional

from frank.base.sqlite_lock import sqlite_write_lock
from frank.hub import state
from frank.hub.database import SOLE_INTERFACE, InterfacePreferenceRecord

# What a fresh install answers with, and what a daemon whose row has never been written falls back to.
DEFAULTS: dict[str, Any] = {
    "color_mode": "system",
    "locale": "",
    "last_workspace_id": "",
    "computer_control_awaiting_grant": False,
}

# The colour modes the interface can be in.
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
    """Everything the interface remembers, as one object.

    All of it at once because the interface wants all of it at once: it reads this before its
    first paint, and a request per preference would be a round trip per preference between a
    blank screen and a themed one.
    """
    assert state.session_factory is not None
    database = state.session_factory()
    try:
        return _as_payload(database.get(InterfacePreferenceRecord, SOLE_INTERFACE))
    finally:
        database.close()


def update_preferences(changes: dict[str, Any]) -> dict[str, Any]:
    """Apply a partial update and answer with the whole of what is now stored.

    Partial because the interface changes one of these at a time — a theme toggle knows nothing
    about the locale, and making it send one would let a stale copy overwrite a change made in
    another window between the read and the write.
    """
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
