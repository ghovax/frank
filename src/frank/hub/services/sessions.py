"""Session domain: session listing and drafts, permission-mode resolution, pending-input handling, title generation, and workspace setup."""

from __future__ import annotations

from datetime import datetime, timezone
from fastapi import HTTPException
from frank.base.worktrees import SessionWorktree, WorktreeStrategy
from frank.base.sqlite_lock import sqlite_write_lock
from pathlib import Path
from typing import Any, cast
from frank.hub import state
from frank.hub.database import SessionRecord
from frank.hub.services.broadcast import _publish_broadcast


def claim_work_habits_acknowledgement(session_id: str) -> bool:
    """Claim the one-time work-habits acknowledgement for a session."""
    if state.session_store is None:
        return False
    return state.session_store.claim_work_habits_acknowledgement(session_id)


def _reset_work_habits_acknowledgements() -> None:
    """Allow one fresh acknowledgement after the work-habits setting changes."""
    if state.session_store is not None:
        state.session_store.reset_work_habits_acknowledgements()


def _normalize_permission_mode(mode: str) -> str:
    """A stored mode, as the enum reads it."""
    from frank.base.permission_mode import PermissionMode

    return str(PermissionMode.coerce(mode))


def _session_worktree_from_record(record: SessionRecord) -> SessionWorktree:
    source = cast(str, record.working_directory) or ""
    runtime = cast(str, record.runtime_working_directory) or source
    strategy = cast(str, record.worktree_strategy) or "none"
    worktree_strategy = cast(WorktreeStrategy, strategy if strategy in {"none", "branch", "worktree"} else "none")
    return SessionWorktree(
        source_working_directory=source,
        runtime_working_directory=runtime,
        strategy=worktree_strategy,
        worktree_path=cast(str, record.worktree_path) or "",
        worktree_branch=cast(str, record.worktree_branch) or "",
        source_repository_root=cast(str, record.source_repository_root) or "",
        runtime_repository_root=cast(str, record.runtime_repository_root) or "",
        head=cast(str, record.worktree_head) or "",
        error=cast(str, record.worktree_error) or "",
    )


def _ensure_session_workspace(
    session_id: str,
    agent: str,
    working_directory: str,
    worktree_strategy: str,
    permission_mode: str,
    workspace_id: str = "",
) -> SessionWorktree:
    """Give a session its durable row and the directory its tools will actually run in."""
    assert state.session_factory is not None
    source_directory = working_directory or str(Path.home())

    database_session = state.session_factory()
    try:
        record = database_session.get(SessionRecord, session_id)
        if record is not None:
            workspace = _session_worktree_from_record(record)
            if workspace.runtime_working_directory:
                return workspace
    finally:
        database_session.close()

    requested_strategy = worktree_strategy if worktree_strategy in {"none", "branch", "worktree"} else ""
    strategy = cast(WorktreeStrategy, requested_strategy or (state.global_configuration.workspace.strategy if state.global_configuration is not None else "none"))
    if state.worktree_manager is not None:
        workspace = state.worktree_manager.prepare_sync(session_id, source_directory, strategy)
    else:
        resolved = str(Path(source_directory).expanduser().resolve(strict=False))
        workspace = SessionWorktree(
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
                    record.worktree_strategy = workspace.strategy
                    record.worktree_path = workspace.worktree_path
                    record.worktree_branch = workspace.worktree_branch
                    record.source_repository_root = workspace.source_repository_root
                    record.runtime_repository_root = workspace.runtime_repository_root
                    record.worktree_head = workspace.head
                    record.worktree_error = workspace.error
                    database_session.commit()
                return _session_worktree_from_record(record)
            # No title yet: the session names itself once it has read its first message, which is the only point anything knows what the session is for.
            database_session.add(SessionRecord(
                id=session_id,
                agent=agent,
                workspace_id=workspace_id,
                working_directory=workspace.source_working_directory,
                runtime_working_directory=workspace.runtime_working_directory,
                worktree_strategy=workspace.strategy,
                worktree_path=workspace.worktree_path,
                worktree_branch=workspace.worktree_branch,
                source_repository_root=workspace.source_repository_root,
                runtime_repository_root=workspace.runtime_repository_root,
                worktree_head=workspace.head,
                worktree_error=workspace.error,
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


def _activity_of(session_id: str, lifecycle: str) -> str:
    """What a session is doing, in the order of interest."""
    if lifecycle == "ended":
        return "ended"
    if session_id in state._awaiting_input_contexts:
        return "waiting"
    if session_id in state._running_contexts:
        return "working"
    return "idle"


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
                    "workspace_id": row.workspace_id or "",
                    "agent": row.agent,
                    "title": row.title,
                    "created_at": row.created_at,
                    "working_directory": row.working_directory,
                    "runtime_working_directory": row.runtime_working_directory or row.working_directory,
                    "worktree_strategy": row.worktree_strategy or "none",
                    "worktree_path": row.worktree_path or "",
                    "worktree_branch": row.worktree_branch or "",
                    "source_repository_root": row.source_repository_root or "",
                    "runtime_repository_root": row.runtime_repository_root or "",
                    "worktree_head": row.worktree_head or "",
                    "worktree_error": row.worktree_error or "",
                    "permission_mode": _normalize_permission_mode(row.permission_mode or "ask"),
                    "input_draft": row.input_draft or "",
                    "filesystem_leases": (
                        state.file_lease_manager.active_for_session(row.id)
                        if state.file_lease_manager is not None
                        else []
                    ),
                    "running": row.id in state._running_contexts,
                    "awaiting_input": row.id in state._awaiting_input_contexts,
                    # What this session is working toward, when it has said.
                    "goal": state._session_goals.get(row.id),
                    # What the session is *doing*, which `lifecycle` deliberately does not say.
                    "activity": _activity_of(row.id, str(row.lifecycle or "live")),
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
    """Synchronous draft write — MUST run off the event loop (dispatched via ``asyncio.to_thread``)."""
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
