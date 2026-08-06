"""The location domain: serialization, and the session-location resolution the workspace service shares."""

from __future__ import annotations

from datetime import datetime, timezone
from frank.locations.resolver import host_is_defined, location_uri_for, LocationAddress
from frank.protocol.dtos import LocationInput
from itertools import combinations
from pathlib import Path
from typing import Any
import uuid
from frank.hub import state
from frank.hub.database import LocationRecord, WorkspaceRecord, SessionRecord


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _location_address(record: LocationRecord) -> LocationAddress:
    return LocationAddress(kind=record.kind, base_directory=record.base_directory, host_alias=record.host_alias or "")


def _serialize_location(record: LocationRecord) -> dict[str, Any]:
    """A location for the API: its generated URI, derived name, connection and execution policy."""
    try:
        uri = location_uri_for(_location_address(record))
    except Exception:
        uri = ""
    host_known = record.kind == "local" or (bool(record.host_alias) and host_is_defined(record.host_alias))
    return {
        "id": record.id,
        "workspace_id": record.workspace_id,
        "name": record.name,
        "kind": record.kind,
        "host_alias": record.host_alias or "",
        "host_known": host_known,
        "base_directory": record.base_directory,
        "uri": uri,
        "permission_mode": record.permission_mode or "ask",
        "created_at": record.created_at,
    }


def _serialize_workspace(record: WorkspaceRecord, database_session, *, with_locations: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": record.id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "last_session_id": record.last_session_id or "",
    }
    if with_locations:
        locations = (
            database_session.query(LocationRecord)
            .filter(LocationRecord.workspace_id == record.id)
            .order_by(LocationRecord.created_at.asc())
            .all()
        )
        payload["locations"] = [_serialize_location(location) for location in locations]
    session_count = database_session.query(SessionRecord).filter(SessionRecord.workspace_id == record.id).count()
    payload["session_count"] = session_count
    return payload


def _derive_location_name(database_session, workspace_id: str, kind: str, base_directory: str, host_alias: str, *, exclude_id: str = "") -> str:
    """The agent-facing name for a location, derived from its connection rather than entered."""
    if kind == "remote":
        base = (host_alias or "").strip() or "remote"
    else:
        base = Path(base_directory.strip().rstrip("/")).name or "local"
    existing = {
        row.name for row in database_session.query(LocationRecord.name)
        .filter(LocationRecord.workspace_id == workspace_id, LocationRecord.id != exclude_id).all()
    }
    if base not in existing:
        return base
    index = 2
    while f"{base}-{index}" in existing:
        index += 1
    return f"{base}-{index}"


def _location_pair_conflict(first: tuple[str, str, str], second: tuple[str, str, str]) -> str | None:
    """The overlap message for one pair of locations, or `None` when they do not conflict."""
    (machine_a, path_a, raw_a), (machine_b, path_b, raw_b) = first, second
    if machine_a != machine_b or not path_a or not path_b:
        return None
    if path_a == path_b:
        return f"Two environments use the same directory {raw_a}. Each environment must be a distinct place, so remove one or point it somewhere else."
    if path_b.startswith(path_a + "/"):
        return f"{raw_b} is inside {raw_a}, so the two overlap. An environment already covers everything beneath it — give each one its own separate directory."
    if path_a.startswith(path_b + "/"):
        return f"{raw_a} is inside {raw_b}, so the two overlap. An environment already covers everything beneath it — give each one its own separate directory."
    return None


def _locations_conflict_message(entries: list[tuple[str, str, str]]) -> str | None:
    """A message for the first pair of locations that overlap on the same machine."""
    normalized = [
        (
            f"remote:{(host or '').strip()}" if kind == "remote" else "local",
            base.strip().rstrip("/"),
            base.strip(),
        )
        for kind, host, base in entries
    ]
    return next(
        (message for first, second in combinations(normalized, 2) if (message := _location_pair_conflict(first, second))),
        None,
    )


def _existing_location_entries(database_session, workspace_id: str, *, exclude_id: str = "") -> list[tuple[str, str, str]]:
    rows = (
        database_session.query(LocationRecord)
        .filter(LocationRecord.workspace_id == workspace_id, LocationRecord.id != exclude_id)
        .all()
    )
    return [(row.kind, row.host_alias or "", row.base_directory) for row in rows]


def _add_location_row(database_session, workspace_id: str, location_input: LocationInput) -> LocationRecord:
    kind = location_input.kind if location_input.kind in ("local", "remote") else "local"
    host_alias = (location_input.host_alias or "").strip()
    base_directory = location_input.base_directory.strip()
    record = LocationRecord(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        name=_derive_location_name(database_session, workspace_id, kind, base_directory, host_alias),
        kind=kind,
        host_alias=host_alias,
        base_directory=base_directory,
        permission_mode=location_input.permission_mode or "ask",
        created_at=_iso_now(),
    )
    database_session.add(record)
    return record


def _workspace_id_for_location(location_id: str) -> str:
    """Which workspace a location belongs to, read before a delete while there is still something to ask."""
    if state.session_factory is None:
        return ""
    database_session = state.session_factory()
    try:
        record = database_session.get(LocationRecord, location_id)
        return record.workspace_id if record is not None else ""
    finally:
        database_session.close()


def _resolve_session_locations(session_id: str) -> list[dict[str, Any]] | None:
    """The runtime-shaped locations for a session's workspace, with each entry's effective settings."""
    if state.session_factory is None:
        return None
    database_session = state.session_factory()
    try:
        session = database_session.get(SessionRecord, session_id)
        if session is None or not session.workspace_id:
            return None
        workspace = database_session.get(WorkspaceRecord, session.workspace_id)
        if workspace is None:
            return None
        locations = (
            database_session.query(LocationRecord)
            .filter(LocationRecord.workspace_id == workspace.id)
            .order_by(LocationRecord.created_at.asc())
            .all()
        )
        resolved: list[dict[str, Any]] = []
        for location in locations:
            try:
                uri = location_uri_for(_location_address(location))
            except Exception:
                uri = ""
            resolved.append({
                "uri": uri,
                "name": location.name,
                "kind": location.kind,
                "base_directory": location.base_directory,
                "host_alias": location.host_alias or "",
                "permission_mode": location.permission_mode or "ask",
            })
        return resolved or None
    finally:
        database_session.close()
