"""The other Franks this one knows how to reach.

A phone keeps this set in its keychain and shows it as a list of machines. This is the same set
for the desktop, kept where the desktop keeps things — so "which Frank" is a question with the
same answer and the same shape on both, rather than a feature one client has.

Added from the `frank://pair#…` link `frank reach` prints, parsed here rather than in the
interface. There is one encoding of a machine's identity and it should be read in one place; a
second reader in TypeScript is how the two come to disagree about what a version field means.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from frank.base.sqlite_lock import sqlite_write_lock
from frank.hub import state
from frank.hub.database import MachineRecord

# The scheme `frank reach` prints.
PAIRING_PREFIX = "frank://pair#"


class PairingLinkError(ValueError):
    """A pairing link that could not be read, with a sentence saying which way."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_pairing_link(link: str) -> dict[str, str]:
    """Turn a `frank://pair#…` link into the machine it describes.

    The payload is base64url with its padding stripped, which is what makes the link short enough
    to be a QR code; `b64decode` wants the padding back."""
    fragment = link.strip()
    if fragment.startswith(PAIRING_PREFIX):
        fragment = fragment[len(PAIRING_PREFIX):]
    elif "#" in fragment:
        fragment = fragment.split("#", 1)[1]
    if not fragment:
        raise PairingLinkError("That is not a Frank pairing link.")
    try:
        padded = fragment + "=" * (-len(fragment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PairingLinkError("That is not a Frank pairing link.") from error
    if not isinstance(payload, dict):
        raise PairingLinkError("That is not a Frank pairing link.")
    endpoint = str(payload.get("endpoint") or "").rstrip("/")
    token = str(payload.get("token") or "")
    if not endpoint or not token:
        raise PairingLinkError("That link is missing its address or its token.")
    return {"name": str(payload.get("name") or endpoint), "endpoint": endpoint, "token": token}


def _serialize(record: MachineRecord) -> dict[str, Any]:
    """A machine as the interface sees it.

    The token is **not** here. The interface needs it exactly once, to open the machine, and that
    is its own route — so the list that renders on screen, gets logged by a fetch inspector and
    sits in a memory snapshot carries no credential at all.
    """
    return {
        "id": record.id,
        "name": record.name,
        "endpoint": record.endpoint,
        "created_at": record.created_at,
    }


def machines_payload() -> dict[str, list[dict[str, Any]]]:
    assert state.session_factory is not None
    database = state.session_factory()
    try:
        records = database.query(MachineRecord).order_by(MachineRecord.created_at).all()
        return {"machines": [_serialize(record) for record in records]}
    finally:
        database.close()


def remember_machine(link: str) -> dict[str, Any]:
    """Add a machine, or refresh the token of one already known.

    Keyed on the address, because one `frank reach` is one address. Pairing the same machine again
    after `frank reach rotate` is the ordinary way to hand over a new token, and it should not
    leave the old row behind holding a key that no longer opens anything.
    """
    described = read_pairing_link(link)
    assert state.session_factory is not None
    with sqlite_write_lock():
        database = state.session_factory()
        try:
            record = (
                database.query(MachineRecord)
                .filter(MachineRecord.endpoint == described["endpoint"])
                .first()
            )
            if record is None:
                record = MachineRecord(
                    id=str(uuid.uuid4()),
                    name=described["name"],
                    endpoint=described["endpoint"],
                    token=described["token"],
                    created_at=_now(),
                )
                database.add(record)
            else:
                # The name is left alone.
                record.token = described["token"]
            database.commit()
            return _serialize(record)
        finally:
            database.close()


def machine_address(machine_id: str) -> str:
    """The one URL that opens a machine, token and all, or `""` if there is no such machine.

    Its own call because it is the only thing here that hands out a credential. The interface asks
    for it at the moment somebody chooses to go, rather than holding it from the moment the list
    rendered."""
    assert state.session_factory is not None
    database = state.session_factory()
    try:
        record = database.get(MachineRecord, machine_id)
        if record is None:
            return ""
        from urllib.parse import quote

        return f"{record.endpoint}/?token={quote(record.token, safe='')}"
    finally:
        database.close()


def rename_machine(machine_id: str, name: str) -> dict[str, Any] | None:
    trimmed = name.strip()
    if not trimmed:
        return None
    assert state.session_factory is not None
    with sqlite_write_lock():
        database = state.session_factory()
        try:
            record = database.get(MachineRecord, machine_id)
            if record is None:
                return None
            record.name = trimmed
            database.commit()
            return _serialize(record)
        finally:
            database.close()


def forget_machine(machine_id: str) -> bool:
    assert state.session_factory is not None
    with sqlite_write_lock():
        database = state.session_factory()
        try:
            record = database.get(MachineRecord, machine_id)
            if record is None:
                return False
            database.delete(record)
            database.commit()
            return True
        finally:
            database.close()


__all__ = [
    "PairingLinkError",
    "forget_machine",
    "machine_address",
    "machines_payload",
    "read_pairing_link",
    "remember_machine",
    "rename_machine",
]
