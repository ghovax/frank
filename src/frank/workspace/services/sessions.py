"""Session domain: session listing and drafts, permission-mode resolution, pending-input
handling, title generation, and workspace setup."""

from __future__ import annotations

from datetime import datetime, timezone
from fastapi import HTTPException
from frank.base.workspaces import SessionWorkspace, WorkspaceStrategy
from frank.base.sqlite_lock import sqlite_write_lock
from pathlib import Path
from typing import Any, cast
from frank.workspace import state
from frank.workspace.database import SessionRecord
from frank.workspace.services.broadcast import _publish_broadcast


def claim_work_habits_acknowledgement(session_id: str) -> bool:
    """Claim the one-time work-habits acknowledgement for a session.

    Durable, and it has to be: a worker is per activation now, so a session that slept and woke
    would show the acknowledgement again on every wake if the flag lived in the worker."""
    if state.session_store is None:
        return False
    return state.session_store.claim_work_habits_acknowledgement(session_id)


def _reset_work_habits_acknowledgements() -> None:
    """Allow one fresh acknowledgement after the work-habits setting changes."""
    if state.session_store is not None:
        state.session_store.reset_work_habits_acknowledgements()


def _normalize_permission_mode(mode: str) -> str:
    return mode if mode in {"default", "auto", "read_only"} else "default"


def _session_workspace_from_record(record: SessionRecord) -> SessionWorkspace:
    source = cast(str, record.working_directory) or ""
    runtime = cast(str, record.runtime_working_directory) or source
    strategy = cast(str, record.workspace_strategy) or "none"
    workspace_strategy = cast(WorkspaceStrategy, strategy if strategy in {"none", "branch", "worktree"} else "none")
    return SessionWorkspace(
        source_working_directory=source,
        runtime_working_directory=runtime,
        strategy=workspace_strategy,
        workspace_path=cast(str, record.workspace_path) or "",
        workspace_branch=cast(str, record.workspace_branch) or "",
        source_repository_root=cast(str, record.source_repository_root) or "",
        runtime_repository_root=cast(str, record.runtime_repository_root) or "",
        head=cast(str, record.workspace_head) or "",
        error=cast(str, record.workspace_error) or "",
    )


def _ensure_session_workspace(
    session_id: str,
    agent: str,
    working_directory: str,
    workspace_strategy: str,
    permission_mode: str,
    project_id: str = "",
) -> SessionWorkspace:
    """Give a session its durable row and the directory its tools will actually run in.

    Called when the session is created, not on its first turn: the workspace decides where
    every tool runs, and a session that exists but has not yet resolved where it lives is a
    session nobody can address properly. Idempotent — a row that already has a runtime
    directory is returned as it stands."""
    assert state.session_factory is not None
    source_directory = working_directory or str(Path.home())

    database_session = state.session_factory()
    try:
        record = database_session.get(SessionRecord, session_id)
        if record is not None:
            workspace = _session_workspace_from_record(record)
            if workspace.runtime_working_directory:
                return workspace
    finally:
        database_session.close()

    requested_strategy = workspace_strategy if workspace_strategy in {"none", "branch", "worktree"} else ""
    strategy = cast(WorkspaceStrategy, requested_strategy or (state.global_configuration.workspace.strategy if state.global_configuration is not None else "none"))
    if state.workspace_manager is not None:
        workspace = state.workspace_manager.prepare_sync(session_id, source_directory, strategy)
    else:
        resolved = str(Path(source_directory).expanduser().resolve(strict=False))
        workspace = SessionWorkspace(
            source_working_directory=resolved,
            runtime_working_directory=resolved,
            strategy="none",
            error="Session workspace manager is not initialized.",
        )

    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            record = database_session.get(SessionRecord, session_id)
            if record is not None:
                if not record.runtime_working_directory:
                    record.runtime_working_directory = workspace.runtime_working_directory
                    record.workspace_strategy = workspace.strategy
                    record.workspace_path = workspace.workspace_path
                    record.workspace_branch = workspace.workspace_branch
                    record.source_repository_root = workspace.source_repository_root
                    record.runtime_repository_root = workspace.runtime_repository_root
                    record.workspace_head = workspace.head
                    record.workspace_error = workspace.error
                    database_session.commit()
                return _session_workspace_from_record(record)
            # No title yet: the session names itself once it has read its first message,
            # which is the only point anything knows what the session is for.
            database_session.add(SessionRecord(
                id=session_id,
                agent=agent,
                project_id=project_id,
                working_directory=workspace.source_working_directory,
                runtime_working_directory=workspace.runtime_working_directory,
                workspace_strategy=workspace.strategy,
                workspace_path=workspace.workspace_path,
                workspace_branch=workspace.workspace_branch,
                source_repository_root=workspace.source_repository_root,
                runtime_repository_root=workspace.runtime_repository_root,
                workspace_head=workspace.head,
                workspace_error=workspace.error,
                permission_mode=_normalize_permission_mode(permission_mode),
                input_draft="",
                title="",
                created_at=datetime.now(timezone.utc).isoformat(),
            ))
            database_session.commit()
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()

    _publish_broadcast({"type": "sessions_changed"})
    return workspace




def _set_session_title(session_id: str, title: str) -> bool:
    assert state.session_factory is not None
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            record = database_session.get(SessionRecord, session_id)
            if record is None or record.title == title:
                return False
            record.title = title
            database_session.commit()
            return True
        except Exception:
            database_session.rollback()
            return False
        finally:
            database_session.close()


def _sessions_payload() -> dict[str, list[dict[str, Any]]]:
    """List recent chat sessions for the sidebar."""
    assert state.session_factory is not None
    database_session = state.session_factory()
    try:
        rows = database_session.query(SessionRecord).order_by(SessionRecord.created_at.desc()).limit(50).all()
        return {
            "sessions": [
                {
                    "session_id": row.id,
                    "project_id": row.project_id or "",
                    "agent": row.agent,
                    "title": row.title,
                    "created_at": row.created_at,
                    "working_directory": row.working_directory,
                    "runtime_working_directory": row.runtime_working_directory or row.working_directory,
                    "workspace_strategy": row.workspace_strategy or "none",
                    "workspace_path": row.workspace_path or "",
                    "workspace_branch": row.workspace_branch or "",
                    "source_repository_root": row.source_repository_root or "",
                    "runtime_repository_root": row.runtime_repository_root or "",
                    "workspace_head": row.workspace_head or "",
                    "workspace_error": row.workspace_error or "",
                    "permission_mode": _normalize_permission_mode(row.permission_mode or "default"),
                    "input_draft": row.input_draft or "",
                    "filesystem_leases": (
                        state.file_lease_manager.active_for_session(row.id)
                        if state.file_lease_manager is not None
                        else []
                    ),
                    "running": row.id in state._running_contexts,
                    "awaiting_input": row.id in state._awaiting_input_contexts,
                }
                for row in rows
            ]
        }
    finally:
        database_session.close()


def _session_draft(session_id: str) -> str:
    assert state.session_factory is not None
    database_session = state.session_factory()
    try:
        record = database_session.get(SessionRecord, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        return record.input_draft or ""
    finally:
        database_session.close()


def _update_session_draft(session_id: str, input_draft: str) -> None:
    """Synchronous draft write — MUST run off the event loop (dispatched via
    ``asyncio.to_thread``). It takes the synchronous history.db write lock, which the
    async task store holds across its transaction's ``await``; acquiring it on the loop
    thread would deadlock the whole server."""
    assert state.session_factory is not None
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            record = database_session.get(SessionRecord, session_id)
            if record is None:
                raise HTTPException(status_code=404, detail="Session not found.")
            record.input_draft = input_draft
            database_session.commit()
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


def _remove_upload_file(path_string: str, uploads_root: str) -> None:
    """Delete an orphaned content-addressed upload from Frank's uploads directory."""
    path = Path(path_string)
    root = Path(uploads_root)
    if path.parent != root:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
