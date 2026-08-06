"""Workspace domain: workspace and location CRUD, the SSH host registry, and the macOS permission probes."""

from __future__ import annotations

from contextlib import suppress
from frank.base.sqlite_lock import sqlite_write_lock
from frank.locations import ssh_hosts as _ssh_hosts
from frank.protocol.dtos import LocationInput, WorkspaceCreateRequest
from pathlib import Path
from typing import Any
import subprocess
import uuid
from frank.hub import state
from frank.hub.database import LocationRecord, WorkspaceRecord, SessionRecord
from frank.hub.services.locations import _add_location_row, _derive_location_name, _existing_location_entries, _iso_now, _locations_conflict_message, _serialize_location, _serialize_workspace


def _workspace_name(path: str) -> str:
    normalized = path.rstrip("/\\")
    return Path(normalized).name or normalized or path


def _workspaces_payload() -> dict[str, list[dict[str, Any]]]:
    assert state.session_factory is not None
    database_session = state.session_factory()
    try:
        rows = database_session.query(WorkspaceRecord).order_by(WorkspaceRecord.updated_at.desc()).all()
        return {"workspaces": [_serialize_workspace(row, database_session) for row in rows]}
    finally:
        database_session.close()


def _workspace_payload(workspace_id: str) -> dict[str, Any] | None:
    assert state.session_factory is not None
    database_session = state.session_factory()
    try:
        record = database_session.get(WorkspaceRecord, workspace_id)
        return _serialize_workspace(record, database_session) if record is not None else None
    finally:
        database_session.close()


def _create_workspace(request: WorkspaceCreateRequest) -> dict[str, Any]:
    assert state.session_factory is not None
    conflict = _locations_conflict_message([(location.kind, location.host_alias, location.base_directory) for location in request.locations])
    if conflict:
        raise ValueError(conflict)
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            now = _iso_now()
            workspace = WorkspaceRecord(
                id=str(uuid.uuid4()),
                created_at=now,
                updated_at=now,
            )
            database_session.add(workspace)
            for location in request.locations:
                _add_location_row(database_session, workspace.id, location)
            database_session.commit()
            return _serialize_workspace(workspace, database_session)
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


def _ensure_default_project() -> None:
    """Guarantee a location-backed grouping on a fresh install, and a no-op once any workspace exists."""
    assert state.session_factory is not None
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            if database_session.query(WorkspaceRecord).count() > 0:
                return
            now = _iso_now()
            workspace = WorkspaceRecord(
                id=str(uuid.uuid4()),
                created_at=now,
                updated_at=now,
            )
            database_session.add(workspace)
            _add_location_row(
                database_session, workspace.id,
                LocationInput(kind="local", base_directory=str(Path.home())),
            )
            database_session.commit()
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


def _remember_last_session(workspace_id: str, session_id: str) -> bool:
    """Record where a workspace was last opened, checking the session belongs to it rather than trusting."""
    assert state.session_factory is not None
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            workspace = database_session.get(WorkspaceRecord, workspace_id)
            if workspace is None:
                return False
            if session_id:
                session = database_session.get(SessionRecord, session_id)
                if session is None or session.workspace_id != workspace_id:
                    return False
            workspace.last_session_id = session_id
            database_session.commit()
            return True
        finally:
            database_session.close()


def _workspace_count() -> int:
    assert state.session_factory is not None
    database_session = state.session_factory()
    try:
        return database_session.query(WorkspaceRecord).count()
    finally:
        database_session.close()


def _full_disk_access_granted() -> bool:
    """Whether this process can read Full-Disk-Access data, tested against the TCC database itself."""
    protected = Path.home() / "Library" / "Application Support" / "com.apple.TCC" / "TCC.db"
    try:
        with open(protected, "rb") as handle:
            handle.read(1)
        return True
    except OSError:
        return False


def _open_full_disk_access_settings() -> None:
    """Open System Settings at the Full Disk Access pane. Best-effort, and a no-op off macOS."""
    with suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"],
            check=False, timeout=5,
        )


def _delete_workspace(workspace_id: str) -> bool:
    """Delete a workspace and everything under it: locations, sessions, and worktree records."""
    assert state.session_factory is not None
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            workspace = database_session.get(WorkspaceRecord, workspace_id)
            if workspace is None:
                return False
            database_session.query(LocationRecord).filter(LocationRecord.workspace_id == workspace_id).delete()
            database_session.query(SessionRecord).filter(SessionRecord.workspace_id == workspace_id).delete()
            database_session.delete(workspace)
            database_session.commit()
            return True
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


def _create_location(workspace_id: str, request: LocationInput) -> dict[str, Any] | None:
    assert state.session_factory is not None
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            workspace = database_session.get(WorkspaceRecord, workspace_id)
            if workspace is None:
                return None
            conflict = _locations_conflict_message(
                _existing_location_entries(database_session, workspace_id) + [(request.kind, request.host_alias, request.base_directory)]
            )
            if conflict:
                raise ValueError(conflict)
            record = _add_location_row(database_session, workspace_id, request)
            workspace.updated_at = _iso_now()
            database_session.commit()
            return _serialize_location(record)
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


def _update_location(location_id: str, request: LocationInput) -> dict[str, Any] | None:
    assert state.session_factory is not None
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            record = database_session.get(LocationRecord, location_id)
            if record is None:
                return None
            next_kind = request.kind if request.kind in ("local", "remote") else record.kind
            next_base_directory = request.base_directory.strip() or record.base_directory
            next_host_alias = (request.host_alias or "").strip()
            conflict = _locations_conflict_message(
                _existing_location_entries(database_session, record.workspace_id, exclude_id=location_id)
                + [(next_kind, next_host_alias, next_base_directory)]
            )
            if conflict:
                raise ValueError(conflict)
            record.kind = next_kind
            record.host_alias = next_host_alias
            record.base_directory = next_base_directory
            record.permission_mode = request.permission_mode or "ask"
        # The name follows the connection, so re-derive it whenever the connection changes.
            record.name = _derive_location_name(
                database_session, record.workspace_id, record.kind, record.base_directory, record.host_alias, exclude_id=record.id
            )
            workspace = database_session.get(WorkspaceRecord, record.workspace_id)
            if workspace is not None:
                workspace.updated_at = _iso_now()
            database_session.commit()
            return _serialize_location(record) if workspace is not None else None
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


def _delete_location(location_id: str) -> bool:
    assert state.session_factory is not None
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            record = database_session.get(LocationRecord, location_id)
            if record is None:
                return False
            database_session.delete(record)
            database_session.commit()
            return True
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


def _hosts_payload() -> dict[str, list[dict[str, Any]]]:
    hosts = _ssh_hosts.list_ssh_hosts()
    return {
        "hosts": [
            {"alias": host.alias, "hostname": host.hostname, "user": host.user, "port": host.port, "identity_files": list(host.identity_files)}
            for host in hosts
        ]
    }


async def _reset_all_runtimes() -> None:
    """Drop every cached runtime, for a change the configuration watcher cannot see — a sign-in token."""
    await state.reset_runtimes()
