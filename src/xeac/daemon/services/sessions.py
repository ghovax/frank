"""Session domain: session listing and drafts, permission-mode resolution, pending-input
handling, title generation, and workspace setup."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from fastapi import HTTPException
from xeac.base.configuration import PromptLoader
from xeac.base.configuration import load_agent_configuration
from xeac.base.workspaces import SessionWorkspace
from xeac.base.workspaces import WorkspaceStrategy
from xeac.base.sqlite_lock import sqlite_write_lock
from xeac.protocol.dtos import SessionTitle
from langchain_core.messages import HumanMessage
from langchain_core.messages import SystemMessage
from pathlib import Path
from typing import Any
from typing import cast
import asyncio
import xeac.base.configuration as _configuration
import logging
from xeac.daemon import state
from xeac.daemon.persistence.database import SessionLifecycleRecord, SessionRecord
from xeac.daemon.services.broadcast import _publish_broadcast


def _schedule_session_title(context_id: str, first_message: str) -> None:
    if state.main_loop is not None and state.main_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(_finalize_session_title(context_id, first_message), state.main_loop)
        state._title_tasks.add(future)
        future.add_done_callback(state._title_tasks.discard)
        return
    try:
        task = asyncio.create_task(_finalize_session_title(context_id, first_message))
        state._title_tasks.add(task)
        task.add_done_callback(state._title_tasks.discard)
    except RuntimeError:
        # No running event loop (e.g. called outside a request) — keep the provisional title.
        pass


def _claim_work_habits_acknowledgement(context_id: str) -> bool:
    """Atomically claim the one-time work-habits acknowledgement for a session."""
    if state.session_factory is None or not context_id:
        return False
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            record = database_session.get(SessionLifecycleRecord, context_id)
            if record is not None and record.work_habits_acknowledged_at:
                return False
            acknowledged_at = datetime.now(timezone.utc).isoformat()
            if record is None:
                database_session.add(
                    SessionLifecycleRecord(
                        context_id=context_id,
                        work_habits_acknowledged_at=acknowledged_at,
                    ),
                )
            else:
                record.work_habits_acknowledged_at = acknowledged_at
            database_session.commit()
            return True
        except Exception:
            database_session.rollback()
            logging.getLogger("xeac.daemon").exception(
                "failed to claim work-habits acknowledgement for %s",
                context_id,
            )
            return False
        finally:
            database_session.close()


def _reset_work_habits_acknowledgements() -> None:
    """Allow one fresh acknowledgement after the work-habits setting changes."""
    if state.session_factory is None:
        return
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            database_session.query(SessionLifecycleRecord).update(
                {SessionLifecycleRecord.work_habits_acknowledged_at: ""},
                synchronize_session=False,
            )
            database_session.commit()
        except Exception:
            database_session.rollback()
            logging.getLogger("xeac.daemon").exception(
                "failed to reset persisted work-habits acknowledgements",
            )
        finally:
            database_session.close()


def _session_agent_for(context_id: str) -> str:
    """Read a session's owning agent from its record (``""`` when unknown). Lets an
    on-demand action reach the right executor even before that agent has a live
    runtime this process (e.g. a session reopened after a restart)."""
    if state.session_factory is None or not context_id:
        return ""
    database_session = state.session_factory()
    try:
        record = database_session.get(SessionRecord, context_id)
        return (record.agent or "") if record is not None else ""
    except Exception:
        return ""
    finally:
        database_session.close()


def _session_working_directory_for(context_id: str) -> str:
    """Read a session's source working directory from its record."""
    if state.session_factory is None or not context_id:
        return ""
    database_session = state.session_factory()
    try:
        record = database_session.get(SessionRecord, context_id)
        return (record.working_directory or "") if record is not None else ""
    except Exception:
        return ""
    finally:
        database_session.close()



async def _resolve_pending_input(
    context_id: str, request_id: str, *,
    decision: str = "", answers: list | None = None, declined: bool = False,
) -> bool:
    """Route a native resolve (permission decision or question answers) into the durable
    input-required resolution the registry owns, so the native REST path and an external
    ``input_response`` message share one resume."""
    if state.registry is None:
        return False
    return await state.registry.resolve_pending_input(
        context_id, request_id, decision=decision, answers=answers, declined=declined,
    )


async def _abort_pending_input(context_id: str) -> bool:
    """Deny every pending interaction of a context's input-required task, resuming it so
    the denials are recorded and the conversation stays valid, instead of stranding a
    checkpoint that a later turn could not build on."""
    if state.registry is None:
        return False
    return await state.registry.abort_pending_input(context_id)


def _normalize_permission_mode(mode: str) -> str:
    return mode if mode in {"default", "auto", "read_only"} else "default"


def _session_permission_mode_for(context_id: str) -> str:
    """Read a context's persisted permission mode for frontend hydration and
    runtime rebuilds. Missing/invalid values fall back to the agent default."""
    if state.session_factory is None or not context_id:
        return "default"
    database_session = state.session_factory()
    try:
        record = database_session.get(SessionRecord, context_id)
        return _normalize_permission_mode(record.permission_mode or "default") if record is not None else "default"
    except Exception:
        return "default"
    finally:
        database_session.close()


def _set_session_permission_mode(context_id: str, mode: str) -> bool:
    """Persist a session permission mode. Returns whether the session exists."""
    if state.session_factory is None or not context_id:
        return False
    normalized = _normalize_permission_mode(mode)
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            record = database_session.get(SessionRecord, context_id)
            if record is None:
                return False
            record.permission_mode = normalized
            database_session.commit()
            return True
        except Exception:
            database_session.rollback()
            return False
        finally:
            database_session.close()


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


def _record_session_visible(context_id: str) -> None:
    _publish_broadcast({"type": "sessions_changed"})


def _ensure_session_workspace(
    context_id: str,
    agent: str,
    working_directory: str,
    workspace_strategy: str,
    permission_mode: str,
    first_message: str,
    project_id: str = "",
) -> SessionWorkspace:
    assert state.session_factory is not None
    source_directory = working_directory or str(Path.home())

    database_session = state.session_factory()
    try:
        record = database_session.get(SessionRecord, context_id)
        if record is not None:
            workspace = _session_workspace_from_record(record)
            if workspace.runtime_working_directory:
                return workspace
    finally:
        database_session.close()

    requested_strategy = workspace_strategy if workspace_strategy in {"none", "branch", "worktree"} else ""
    strategy = cast(WorkspaceStrategy, requested_strategy or (state.global_configuration.workspace.strategy if state.global_configuration is not None else "none"))
    if state.workspace_manager is not None:
        workspace = state.workspace_manager.prepare_sync(context_id, source_directory, strategy)
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
            record = database_session.get(SessionRecord, context_id)
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
            # Provisional title so the sidebar shows something immediately
            title = "" # Empty title
            database_session.add(SessionRecord(
                id=context_id,
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
                title=title,
                created_at=datetime.now(timezone.utc).isoformat(),
            ))
            database_session.commit()
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()

    # Surface the new session immediately (its first turn is already marked
    # running, so the sidebar shows it with a spinner right away).
    _publish_broadcast({"type": "sessions_changed"})
    _schedule_session_title(context_id, first_message)
    return workspace


_title_prompt_loader = PromptLoader(
    Path(_configuration.__file__).resolve().parent / "prompts"
)


async def _generate_session_title(context_id: str, first_message: str) -> str:
    """Ask the configured LLM for a short, structured title for the session.

    ``SessionTitle`` is bound as a tool with auto tool-choice rather than via
    ``with_structured_output``: the configured reasoning model rejects both
    ``response_format`` (json_schema) and the forced ``tool_choice`` that
    ``with_structured_output`` relies on, but accepts a regular tool call — the
    same pattern the main agent uses with ``bind_tools``.
    """
    assert state.global_configuration is not None
    configuration = state.global_configuration
    agent_name = _session_agent_for(context_id) or configuration.default_agent
    working_directory = _session_working_directory_for(context_id)
    agent_directories = (
        configuration.agent_directories_for(working_directory)
        if working_directory
        else configuration.agent_directories()
    )
    agent_configuration = load_agent_configuration(agent_name, agent_directories)
    model_identifier = agent_configuration.model_identifier
    if not model_identifier:
        return ""
    # Auto-titling is a background nicety, so skip silently when the model's provider
    # isn't authorized rather than surfacing an error. Authorization goes through the
    # same central authority the main agent uses, so it covers the native chatgpt
    # (OAuth) provider too — not just LiteLLM api keys, which is why titling used to
    # exclude chatgpt sessions entirely.
    if not model_is_authorized(model_identifier, configuration):
        return ""
    # Build through the shared factory so the title call uses the same provider,
    # auth, and request path as the agent's own turns (the chatgpt subscription route
    # included). Reasoning is dialed down: a title is trivial and must stay cheap and
    # fast, unlike the agent's configured effort.
    title_agent_configuration = agent_configuration.model_copy(update={"reasoning_effort": "low"})
    llm = build_chat_model(
        model_identifier, configuration, title_agent_configuration
    ).bind_tools([SessionTitle], tool_choice="auto")
    prompt = _title_prompt_loader.load("session_title", {})
    response = await llm.ainvoke([
        SystemMessage(content=prompt),
        HumanMessage(content=first_message),
    ])
    if not response.tool_calls:
        return ""
    title = SessionTitle.model_validate(response.tool_calls[0]["args"]).title
    return (title or "").strip()


async def _finalize_session_title(context_id: str, first_message: str) -> None:
    """Generate an LLM title for a new session and update the sidebar record."""
    try:
        title = await _generate_session_title(context_id, first_message)
    except Exception:
        return  # Keep the provisional title on any failure.
    if not title:
        return
    changed = await asyncio.to_thread(_set_session_title, context_id, title)
    if changed:
        state.broadcaster.publish({"type": "sessions_changed"})


def _set_session_title(context_id: str, title: str) -> bool:
    assert state.session_factory is not None
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            record = database_session.get(SessionRecord, context_id)
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


def _session_draft(context_id: str) -> str:
    assert state.session_factory is not None
    database_session = state.session_factory()
    try:
        record = database_session.get(SessionRecord, context_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        return record.input_draft or ""
    finally:
        database_session.close()


def _update_session_draft(context_id: str, input_draft: str) -> None:
    """Synchronous draft write — MUST run off the event loop (dispatched via
    ``asyncio.to_thread``). It takes the synchronous history.db write lock, which the
    async task store holds across its transaction's ``await``; acquiring it on the loop
    thread would deadlock the whole server."""
    assert state.session_factory is not None
    with sqlite_write_lock():
        database_session = state.session_factory()
        try:
            record = database_session.get(SessionRecord, context_id)
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
    """Delete an orphaned content-addressed upload from XEAC's uploads directory."""
    path = Path(path_string)
    root = Path(uploads_root)
    if path.parent != root:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
