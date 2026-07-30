"""Project domain: project and location CRUD, the SSH host registry, and the macOS
permission probes (full-disk-access, accessibility)."""

from __future__ import annotations

from contextlib import suppress
from frank.base.sqlite_lock import sqlite_write_lock
from frank.locations import ssh_hosts as _ssh_hosts
from frank.protocol.dtos import LocationInput, ProjectCreateRequest
from pathlib import Path
from typing import Any
import subprocess
import uuid
from frank.workspace import state
from frank.workspace.database import LocationRecord, ProjectRecord, SessionRecord
from frank.workspace.services.locations import _add_location_row, _derive_location_name, _existing_location_entries, _iso_now, _locations_conflict_message, _serialize_location, _serialize_project


def _project_name(path: str) -> str:
    normalized = path.rstrip("/\\")
    return Path(normalized).name or normalized or path


def _projects_payload() -> dict[str, list[dict[str, Any]]]:
    assert state.session_factory is not None
    database_session = state.session_factory()
    try:
        rows = database_session.query(ProjectRecord).order_by(ProjectRecord.updated_at.desc()).all()
        return {"projects": [_serialize_project(row, database_session) for row in rows]}
    finally:
        database_session.close()


def _project_payload(project_id: str) -> dict[str, Any] | None:
    assert state.session_factory is not None
    database_session = state.session_factory()
    try:
        record = database_session.get(ProjectRecord, project_id)
        return _serialize_project(record, database_session) if record is not None else None
    finally:
        database_session.close()


def _create_project(request: ProjectCreateRequest) -> dict[str, Any]:
    assert state.session_factory is not None
    conflict = _locations_conflict_message([(location.kind, location.host_alias, location.base_directory) for location in request.locations])
    if conflict:
        raise ValueError(conflict)
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            now = _iso_now()
            project = ProjectRecord(
                id=str(uuid.uuid4()),
                created_at=now,
                updated_at=now,
            )
            database_session.add(project)
            for location in request.locations:
                _add_location_row(database_session, project.id, location)
            database_session.commit()
            return _serialize_project(project, database_session)
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


def _ensure_default_project() -> None:
    """Guarantee the app has a location-backed grouping on a fresh install.

    The initial location targets the daemon user's home directory. This is a no-op once any
    project exists, so it never changes user-created groupings.
    """
    assert state.session_factory is not None
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            if database_session.query(ProjectRecord).count() > 0:
                return
            now = _iso_now()
            project = ProjectRecord(
                id=str(uuid.uuid4()),
                created_at=now,
                updated_at=now,
            )
            database_session.add(project)
            _add_location_row(
                database_session, project.id,
                LocationInput(kind="local", base_directory=str(Path.home())),
            )
            database_session.commit()
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


def _project_count() -> int:
    assert state.session_factory is not None
    database_session = state.session_factory()
    try:
        return database_session.query(ProjectRecord).count()
    finally:
        database_session.close()


def _full_disk_access_granted() -> bool:
    """Whether *this* process can read Full-Disk-Access-protected data, tested by trying to
    read a byte of the user's TCC database (a canonical FDA-gated file). Reflects the reality
    the user-context probe faces: in the packaged app FDA is attributed to Frank.app (the
    responsible parent of the daemon), so this flips true once the user grants it. Any
    permission/OS error means no access."""
    protected = Path.home() / "Library" / "Application Support" / "com.apple.TCC" / "TCC.db"
    try:
        with open(protected, "rb") as handle:
            handle.read(1)
        return True
    except OSError:
        return False


def _open_full_disk_access_settings() -> None:
    """Open System Settings straight to the Full Disk Access pane so the user can add Frank in
    one hop. Best-effort; a non-macOS or failed ``open`` is simply a no-op."""
    with suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"],
            check=False, timeout=5,
        )


def _delete_project(project_id: str) -> bool:
    """Delete a project and everything under it: its locations, its sessions, and the
    per-(session, location) worktree records. (Remote worktree teardown over SSH is a
    follow-up — the DB rows go now.)"""
    assert state.session_factory is not None
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            project = database_session.get(ProjectRecord, project_id)
            if project is None:
                return False
            database_session.query(LocationRecord).filter(LocationRecord.project_id == project_id).delete()
            database_session.query(SessionRecord).filter(SessionRecord.project_id == project_id).delete()
            database_session.delete(project)
            database_session.commit()
            return True
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


def _create_location(project_id: str, request: LocationInput) -> dict[str, Any] | None:
    assert state.session_factory is not None
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            project = database_session.get(ProjectRecord, project_id)
            if project is None:
                return None
            conflict = _locations_conflict_message(
                _existing_location_entries(database_session, project_id) + [(request.kind, request.host_alias, request.base_directory)]
            )
            if conflict:
                raise ValueError(conflict)
            record = _add_location_row(database_session, project_id, request)
            project.updated_at = _iso_now()
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
                _existing_location_entries(database_session, record.project_id, exclude_id=location_id)
                + [(next_kind, next_host_alias, next_base_directory)]
            )
            if conflict:
                raise ValueError(conflict)
            record.kind = next_kind
            record.host_alias = next_host_alias
            record.base_directory = next_base_directory
            record.permission_mode = request.permission_mode or "default"
            # The name follows the connection, so re-derive it (deduped, excluding this row)
            # whenever the connection changes.
            record.name = _derive_location_name(
                database_session, record.project_id, record.kind, record.base_directory, record.host_alias, exclude_id=record.id
            )
            project = database_session.get(ProjectRecord, record.project_id)
            if project is not None:
                project.updated_at = _iso_now()
            database_session.commit()
            return _serialize_location(record) if project is not None else None
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
    """Drop every live session's cached runtime so the next turn rebuilds its chat model.
    Used when the ChatGPT sign-in state changes, which lives in a token file rather than the
    configuration, so the config watcher never fires for it."""
    await state.reset_runtimes()
