"""The harness server runtime: the FastAPI ``app``, its shared singleton state, the
database/ORM models, and every operation the route modules call.

This module owns the runtime — it imports no route module, so a route handler can import
the ``app``, the shared singletons, and the helper operations from here without the
import cycle that used to run ``app -> routes -> app``. The thin :mod:`harness.server.app`
sits on top: it takes this ``app``, mounts the split routers onto it, and exposes
``run_server``. Per-agent A2A sub-apps are still mounted onto ``app`` here (see
``_mount_agent``); only the REST/route routers are mounted by the entry module.
"""

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import jwt
import uuid
import json
import logging
import os
import posixpath
import platform
import re
import shutil
from itertools import combinations
import subprocess

import httpx
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, cast
from urllib.parse import quote, urljoin, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from harness.server import state
from harness.server.services.terminals import (
    TerminalSessionManager,
)
from harness.server.database import (
    SessionRecord,
    SessionLifecycleRecord,
    ProjectRecord,
    LocationRecord,
    ModelHistoryRecord,
    ArtifactVersionRecord,
    ArtifactFileRecord,
    ArtifactSurfaceRecord,
    ArtifactAnnotationRecord,
    _apply_history_schema,
)
from watchfiles import DefaultFilter, awatch
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from harness.server.models import (
    AgentBashConfigurationResponse,
    AgentConfigurationResponse,
    AgentConfigurationUpdateRequest,
    AgentSpawnConfigurationResponse,
    ArtifactAnnotationSaveRequest,
    LocationInput,
    ProjectCreateRequest,
    SessionTitle,
)

from a2a.server.apps.jsonrpc import A2AFastAPIApplication
from a2a.server.request_handlers import DefaultRequestHandler

from harness.core.a2a_executor import (
    AgentRegistry,
    HarnessAgentExecutor,
    agent_rpc_path,
    build_agent_card,
)
from harness.core.agent import build_chat_model, model_is_authorized
from harness.core.remote_agents import RemoteAgentAuth, RemoteAgentConfiguration, RemoteAgentManager
from harness.core.a2a_files import FileUrlSigner, load_or_create_secret
from harness.core import telemetry as _telemetry
from harness.core.push_notification_store import (
    PersistentPushNotificationConfigurationStore,
    PinnedPushNotificationSender,
)
from harness.core.task_store import AppendOnlyTaskStore
import harness.core.configuration as _configuration
from harness.core.configuration import (
    AgentSidecar,
    GlobalConfiguration,
    PromptLoader,
    agent_configuration_path,
    configuration_file_path,
    database_file_path,
    harness_home_directory,
    list_agent_route_names,
    load_agent_configuration,
    save_api_keys,
    seed_home_agents,
)
from harness.core.composio_router import composio_mcp_servers
from harness.core.mcp_client import MCPClientManager
from harness.core.models import find_model, provider_and_suffix
from harness.core.background import reap_orphaned_processes
from harness.core.file_leases import FileLeaseManager
from harness.core.session_workspaces import SessionWorkspace, SessionWorkspaceManager, WorkspaceStrategy
from harness.core.sqlite_lock import configure_sqlite_lock, sqlite_write_lock
from harness.core.skills import load_skills, skills_for_agent
from harness.locations import ssh_hosts as _ssh_hosts
from harness.locations.executor import LocationExecutor
from harness.locations.resolver import LocationAddress, executor_for, host_is_defined, location_uri_for
from harness.core import artifact_versioning as artifacts
from harness.tools.tools import (
    ASSETS_DIRECTORY,
    cancel_all_background_tasks,
    set_exa_client,
    set_mcp_client_manager,
)
from harness.tools.file_tools import set_firecrawl_client, set_jina_api_key, set_proxy_url
from harness.core.tuning import set_tuning, tuning_from_policy

# Load .env (gitignored) so API keys are available via the environment without
# being stored in the tracked configuration.yaml. Existing env vars win, so a
# direnv-provided environment is not overridden.
load_dotenv()

PUBLIC_BASE_URL = "http://localhost:8822"
AGENT_CARD_PATH = "/.well-known/agent-card.json"

























# The host the server was told to bind to (set by run_server). Used at startup to fail closed
# when exposed on a non-loopback interface without inbound auth.
# Outbound A2A client manager (external agents this harness may delegate to). None until
# startup builds it from remote-agents.json; installed on the registry so make_delegate
# can branch a delegation over the wire.
# Signs short-lived URLs for the A2A file-serving endpoint. Built at startup.
# Persisted push-notification configuration store and sender, shared by every mounted
# agent's handler, so a registered webhook survives a restart.
# Composio Tool Router server(s), provisioned once at startup. Kept separate from
# the mcp.json-derived servers so the file watcher's live reload re-merges them
# instead of dropping Composio whenever mcp.json changes.
# Dialogue history per A2A context, shared across every agent executor so that
# switching the active agent continues the same conversation (the persona is
# applied per-turn on top of this shared history).
# How many executions are running per context, including delegated agents. Drives
# session-stream lifetime and the sidebar spinner; a count handles overlapping work.


def _notify_filesystem_lease_state() -> None:
    _publish_broadcast({"type": "sessions_changed"})
    _publish_broadcast({"type": "filesystem_leases_changed"})






def _publish_stream_event(context_id: str, part) -> None:
    """Executor hook: serialize one structured part and fan it out to live viewers."""
    state._event_bus.publish(context_id, part.model_dump(by_alias=True, exclude_none=True, mode="json"))


def _set_turn_state(context_id: str, running: bool) -> None:
    """Track active turns per context and broadcast on the empty/active edge so the
    sidebar reflects which conversations are currently running."""
    previous = state._running_contexts.get(context_id, 0)
    updated = previous + 1 if running else max(0, previous - 1)
    if updated:
        state._running_contexts[context_id] = updated
    else:
        state._running_contexts.pop(context_id, None)
    if (previous == 0) != (updated == 0):
        state._broadcaster.publish({"type": "sessions_changed"})
    # When the last turn for a context finishes, tell live viewers to do a final
    # refresh and close — the structured-part fan-out is only meaningful mid-turn.
    if not running and updated == 0:
        state._event_bus.complete(context_id)


# Contexts whose latest turn is paused at input-required (durably; also populated on
# startup from persisted input-required tasks, so the marker survives a restart).


def _notify_permission_state(context_id: str, awaiting: bool) -> None:
    """A turn suspended at (or resumed from) an input-required pause — track it durably
    and refresh the sidebar so it can swap the spinner for an attention marker."""
    if awaiting:
        state._awaiting_input_contexts.add(context_id)
    else:
        state._awaiting_input_contexts.discard(context_id)
    state._broadcaster.publish({"type": "sessions_changed"})

# Keeps references to in-flight session-title generation tasks so they are not
# garbage-collected before completing.


def _publish_broadcast(event: dict) -> None:
    """Publish from either the event-loop thread or a worker thread."""
    if state._main_loop is not None and state._main_loop.is_running():
        state._main_loop.call_soon_threadsafe(state._broadcaster.publish, event)
    else:
        state._broadcaster.publish(event)


def _schedule_session_title(context_id: str, first_message: str) -> None:
    if state._main_loop is not None and state._main_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(_finalize_session_title(context_id, first_message), state._main_loop)
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
    if state._session_factory is None or not context_id:
        return False
    with sqlite_write_lock():
        database_session = state._session_factory()
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
            logging.getLogger("harness.server").exception(
                "failed to claim work-habits acknowledgement for %s",
                context_id,
            )
            return False
        finally:
            database_session.close()


def _reset_work_habits_acknowledgements() -> None:
    """Allow one fresh acknowledgement after the work-habits setting changes."""
    if state._session_factory is None:
        return
    with sqlite_write_lock():
        database_session = state._session_factory()
        try:
            database_session.query(SessionLifecycleRecord).update(
                {SessionLifecycleRecord.work_habits_acknowledged_at: ""},
                synchronize_session=False,
            )
            database_session.commit()
        except Exception:
            database_session.rollback()
            logging.getLogger("harness.server").exception(
                "failed to reset persisted work-habits acknowledgements",
            )
        finally:
            database_session.close()


def _session_agent_for(context_id: str) -> str:
    """Read a session's owning agent from its record (``""`` when unknown). Lets an
    on-demand action reach the right executor even before that agent has a live
    runtime this process (e.g. a session reopened after a restart)."""
    if state._session_factory is None or not context_id:
        return ""
    database_session = state._session_factory()
    try:
        record = database_session.get(SessionRecord, context_id)
        return (record.agent or "") if record is not None else ""
    except Exception:
        return ""
    finally:
        database_session.close()


def _session_working_directory_for(context_id: str) -> str:
    """Read a session's source working directory from its record."""
    if state._session_factory is None or not context_id:
        return ""
    database_session = state._session_factory()
    try:
        record = database_session.get(SessionRecord, context_id)
        return (record.working_directory or "") if record is not None else ""
    except Exception:
        return ""
    finally:
        database_session.close()


def _executor_for_context(context_id: str) -> "HarnessAgentExecutor | None":
    """Resolve the executor that owns a context, by the agent recorded on its session.

    This is the correct way to dispatch an operation that targets a session's
    persisted state (its conversation) rather than an in-flight turn: it routes to the
    owner authoritatively, independent of whether a runtime is currently warm. Prefer
    it over broadcasting to every executor and relying on live-runtime membership to
    self-select — that pattern silently no-ops whenever the runtime is cold (e.g. right
    after a restart, before the session has taken a turn)."""
    agent_name = _session_agent_for(context_id)
    return state._executors.get(agent_name) if agent_name else None


async def _resolve_pending_input(
    context_id: str, request_id: str, *,
    decision: str = "", answers: "list | None" = None, declined: bool = False,
) -> bool:
    """Route a native resolve (permission decision or question answers) into the durable
    input-required resolution the registry owns, so the native REST path and an external
    ``input_response`` message share one resume."""
    if state._registry is None:
        return False
    return await state._registry.resolve_pending_input(
        context_id, request_id, decision=decision, answers=answers, declined=declined,
    )


async def _abort_pending_input(context_id: str) -> bool:
    """Deny every pending interaction of a context's input-required task, resuming it so
    the denials are recorded and the conversation stays valid, instead of stranding a
    checkpoint that a later turn could not build on."""
    if state._registry is None:
        return False
    return await state._registry.abort_pending_input(context_id)


def _normalize_permission_mode(mode: str) -> str:
    return mode if mode in {"default", "auto", "read_only", "bypass"} else "default"


def _session_permission_mode_for(context_id: str) -> str:
    """Read a context's persisted permission mode for frontend hydration and
    runtime rebuilds. Missing/invalid values fall back to the agent default."""
    if state._session_factory is None or not context_id:
        return "default"
    database_session = state._session_factory()
    try:
        record = database_session.get(SessionRecord, context_id)
        return _normalize_permission_mode(record.permission_mode or "default") if record is not None else "default"
    except Exception:
        return "default"
    finally:
        database_session.close()


def _set_session_permission_mode(context_id: str, mode: str) -> bool:
    """Persist a session permission mode. Returns whether the session exists."""
    if state._session_factory is None or not context_id:
        return False
    normalized = _normalize_permission_mode(mode)
    with sqlite_write_lock():
        database_session = state._session_factory()
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
    assert state._session_factory is not None
    source_directory = working_directory or str(Path.home())

    database_session = state._session_factory()
    try:
        record = database_session.get(SessionRecord, context_id)
        if record is not None:
            workspace = _session_workspace_from_record(record)
            if workspace.runtime_working_directory:
                return workspace
    finally:
        database_session.close()

    requested_strategy = workspace_strategy if workspace_strategy in {"none", "branch", "worktree"} else ""
    strategy = cast(WorkspaceStrategy, requested_strategy or (state._global_configuration.workspace.strategy if state._global_configuration is not None else "none"))
    if state._workspace_manager is not None:
        workspace = state._workspace_manager.prepare_sync(context_id, source_directory, strategy)
    else:
        resolved = str(Path(source_directory).expanduser().resolve(strict=False))
        workspace = SessionWorkspace(
            source_working_directory=resolved,
            runtime_working_directory=resolved,
            strategy="none",
            error="Session workspace manager is not initialized.",
        )

    with sqlite_write_lock():
        database_session = state._session_factory()
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


def _project_name(path: str) -> str:
    normalized = path.rstrip("/\\")
    return Path(normalized).name or normalized or path


def _path_scope(path_value: str, home_root: Path) -> str:
    """Whether a discovered file is ``global`` (under ``~/.agents``) or
    ``project`` (the selected folder's own ``.agents``)."""
    try:
        return "global" if Path(path_value).resolve().is_relative_to(home_root) else "project"
    except Exception:
        return "global"


def _record_model_selection(model_identifier: str) -> None:
    """Record a model selection in the history (upserting by id), mirroring the
    project-history list. Catalog models use their display label; typed model ids
    derive a readable label from the provider/model value."""
    if not model_identifier or state._session_factory is None:
        return
    definition = find_model(model_identifier)
    split = provider_and_suffix(model_identifier)
    if definition is None and split is None:
        return
    if split is not None:
        provider, suffix = split
    else:
        assert definition is not None
        provider, suffix = definition.provider, definition.identifier.split("/", 1)[1]
    label = definition.name if definition is not None else suffix.replace("/", " / ").replace("-", " ").replace("_", " ").title()
    with sqlite_write_lock():
        database_session = state._session_factory()
        try:
            record = database_session.get(ModelHistoryRecord, model_identifier)
            selected_at = datetime.now(timezone.utc).isoformat()
            if record is None:
                database_session.add(ModelHistoryRecord(
                    model_id=model_identifier,
                    name=label,
                    provider=provider,
                    selected_at=selected_at,
                ))
            else:
                record.name = label
                record.provider = provider
                record.selected_at = selected_at
            database_session.commit()
        except Exception:
            database_session.rollback()
        finally:
            database_session.close()


def _recent_models(limit: int = 8) -> list[dict[str, str]]:
    """Recently selected models, newest first."""
    if state._session_factory is None:
        return []
    database_session = state._session_factory()
    try:
        rows = (
            database_session.query(ModelHistoryRecord)
            .order_by(ModelHistoryRecord.selected_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {"id": row.model_id, "name": row.name, "provider": row.provider}
            for row in rows
        ]
    finally:
        database_session.close()


# Artifact versioning — shadow-git capture of the specific files the agent touches (never a
# folder survey). Capture is silent and best-effort: a write-ish tool call enqueues a request
# and returns immediately; a single background worker drains the queue and runs the git
# plumbing (``core/artifact_versioning``) off-loop against the write's location — local or
# remote — then records the DB index rows and broadcasts so open panels refresh. Failures are
# logged, never fatal (a versioning hiccup must not break the agent's turn).

_artifact_logger = logging.getLogger("harness.artifacts")


class _CaptureRequest:
    """One capture unit handed from the agent runtime to the background worker.

    ``mode`` is ``"track"`` to version specific paths (structured writes / ``open_artifact``)
    or ``"recheck"`` to restage only already-tracked files after a ``bash`` call. For
    ``track``, ``changed_absolute_paths`` are the exact files to version and
    ``original_contents`` maps an absolute path to its pre-edit bytes (so a first edit of a
    pre-existing file keeps its original). ``surface`` (open_artifact) may carry no path (an
    external URL) — then only a tab is recorded, no git history."""

    def __init__(
        self, *, context_id: str, location_uri: str, executor: LocationExecutor,
        base_directory: str, changed_absolute_paths: list[str] | None,
        mode: str = "track", original_contents: dict[str, str] | None = None,
        tool_call_id: str = "", message: str = "capture", surface: dict | None = None,
    ):
        self.context_id = context_id
        self.location_uri = location_uri
        self.executor = executor
        self.base_directory = base_directory
        self.changed_absolute_paths = changed_absolute_paths
        self.mode = mode
        self.original_contents = original_contents or {}
        self.tool_call_id = tool_call_id
        self.message = message
        self.surface = surface




def _artifact_maximum_bytes() -> int:
    """The per-file byte cap above which a write is recorded as a placeholder version."""
    workspace = getattr(state._global_configuration, "workspace", None) if state._global_configuration else None
    return int(getattr(workspace, "artifact_maximum_bytes", None) or artifacts.DEFAULT_MAXIMUM_BYTES)


def _capture_artifacts(
    *, context_id: str, location_uri: str, executor: LocationExecutor, base_directory: str,
    changed_absolute_paths: list[str] | None, mode: str = "track",
    original_contents: dict[str, str] | None = None, tool_call_id: str = "", message: str = "capture",
    surface: dict | None = None,
) -> None:
    """The callback injected into the agent runtime, called after a write-ish tool call.
    Non-blocking: build a request, enqueue, and return so the agent's turn never waits on
    git. Takes keyword arguments (not a request object) so the runtime stays decoupled from
    the server's internal request type."""
    if state._capture_queue is None:
        return
    request = _CaptureRequest(
        context_id=context_id, location_uri=location_uri, executor=executor,
        base_directory=base_directory, changed_absolute_paths=changed_absolute_paths,
        mode=mode, original_contents=original_contents,
        tool_call_id=tool_call_id, message=message, surface=surface,
    )
    if state._main_loop is not None and state._main_loop.is_running():
        state._main_loop.call_soon_threadsafe(state._capture_queue.put_nowait, request)
    else:
        try:
            state._capture_queue.put_nowait(request)
        except asyncio.QueueFull:
            _artifact_logger.warning("capture queue full; dropped a capture for %s", context_id)


async def _capture_worker() -> None:
    assert state._capture_queue is not None
    while True:
        request = await state._capture_queue.get()
        try:
            await asyncio.to_thread(_run_capture, request)
        except Exception:
            _artifact_logger.exception("artifact capture failed for %s", request.context_id)
        finally:
            state._capture_queue.task_done()


def _project_id_for_context(context_id: str) -> str:
    if state._session_factory is None:
        return ""
    session = state._session_factory()
    try:
        record = session.query(SessionRecord).filter(SessionRecord.id == context_id).first()
        return cast(str, record.project_id) if record is not None and record.project_id else ""
    finally:
        session.close()


def _within(path: str, base: str) -> bool:
    if not base:
        return False
    path_n, base_n = posixpath.normpath(path), posixpath.normpath(base)
    return path_n == base_n or path_n.startswith(base_n.rstrip("/") + "/")


def _group_work_trees(base_directory: str, changed_absolute_paths: list[str]) -> dict[str, list[str]]:
    """Map each work-tree to the changed rel paths under it. A path inside base_directory
    belongs to base_directory; anything else gets its own work-tree (its parent dir)."""
    groups: dict[str, list[str]] = {}
    for absolute_path in changed_absolute_paths:
        work_tree = base_directory if _within(absolute_path, base_directory) else posixpath.dirname(absolute_path)
        groups.setdefault(work_tree, []).append(posixpath.relpath(absolute_path, work_tree))
    return groups


def _run_capture(request: "_CaptureRequest") -> None:
    """Off-loop: version exactly the files this request names (never a folder survey),
    record the index rows, upsert any surface, and broadcast if anything changed."""
    project_id = _project_id_for_context(request.context_id)
    maximum_bytes = _artifact_maximum_bytes()
    location_home = request.executor.home_directory()
    changed_any = False

    if request.mode == "recheck":
        # After a bash call: restage only already-tracked files in the location's repo.
        work_tree = request.base_directory
        git_directory = artifacts.git_directory_for(location_home, project_id, work_tree)
        try:
            result = artifacts.recheck_tracked(
                request.executor, git_directory, work_tree, request.context_id,
                maximum_bytes=maximum_bytes, message=request.message,
            )
        except artifacts.VersionStoreError:
            _artifact_logger.exception("recheck failed (%s @ %s)", work_tree, request.location_uri)
            result = None
        if result is not None and result.files:
            _record_capture(request, project_id, git_directory, work_tree, result)
            changed_any = True
        if changed_any:
            _publish_broadcast({"type": "artifact_captured", "session_id": request.context_id})
        return

    # mode == "track": version each explicitly named path (grouped by its work-tree).
    for work_tree, relative_paths in _group_work_trees(request.base_directory, request.changed_absolute_paths or []).items():
        git_directory = artifacts.git_directory_for(location_home, project_id, work_tree)
        original_contents = {
            posixpath.relpath(absolute_path, work_tree): content
            for absolute_path, content in request.original_contents.items()
            if content is not None and _within(absolute_path, work_tree)
        }
        try:
            versions = artifacts.track_paths(
                request.executor, git_directory, work_tree, request.context_id, relative_paths,
                original_contents=original_contents or None, maximum_bytes=maximum_bytes, message=request.message,
            )
        except artifacts.VersionStoreError:
            _artifact_logger.exception("track failed (%s @ %s)", work_tree, request.location_uri)
            continue
        for version in versions:
            if version.files:
                _record_capture(request, project_id, git_directory, work_tree, version)
                changed_any = True

    if request.surface is not None:
        _upsert_surface(request, project_id, location_home)
        changed_any = True
    if changed_any:
        _publish_broadcast({"type": "artifact_captured", "session_id": request.context_id})


def _record_capture(request: "_CaptureRequest", project_id: str, git_directory: str, work_tree: str, result: "artifacts.CommitResult") -> None:
    assert state._session_factory is not None
    now = datetime.now(timezone.utc).isoformat()
    version_id = str(uuid.uuid4())
    with sqlite_write_lock():
        session = state._session_factory()
        try:
            session.add(ArtifactVersionRecord(
                id=version_id, context_id=request.context_id, project_id=project_id,
                location_uri=request.location_uri, git_directory=git_directory, work_tree=work_tree,
                branch=artifacts.branch_reference(request.context_id), commit_sha=result.commit_sha,
                sequence=result.sequence, message=request.message,
                tool_call_id=request.tool_call_id, created_at=now,
            ))
            for changed in result.files:
                session.add(ArtifactFileRecord(
                    id=str(uuid.uuid4()), version_id=version_id, context_id=request.context_id,
                    location_uri=request.location_uri, git_directory=git_directory, work_tree=work_tree,
                    commit_sha=result.commit_sha, relative_path=changed.relative_path, absolute_path=changed.absolute_path,
                    blob_sha=changed.blob_sha, change_type=changed.change_type, size=changed.size,
                    is_placeholder=changed.is_placeholder, created_at=now,
                ))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def _upsert_surface(request: "_CaptureRequest", project_id: str, location_home: str) -> None:
    """Create/refresh the surface (tab) for an ``open_artifact``. Reuses an existing surface
    for the same ``(context, absolute_path)`` so re-opening a file updates one tab. For an
    external-URL artifact (no ``absolute_path``) there is no git history — only the live source."""
    assert state._session_factory is not None
    surface = request.surface or {}
    absolute_path = surface.get("absolute_path", "")
    git_directory = work_tree = relative_path = latest_commit = latest_blob = ""
    if absolute_path:
        work_tree = request.base_directory if _within(absolute_path, request.base_directory) else posixpath.dirname(absolute_path)
        git_directory = artifacts.git_directory_for(location_home, project_id, work_tree)
        relative_path = posixpath.relpath(absolute_path, work_tree)
        # The tracking capture already ran for this path, so the file's latest version is
        # simply the branch head's blob for it.
        latest_commit = artifacts.resolve_reference(request.executor, git_directory, artifacts.branch_reference(request.context_id))
        if latest_commit:
            latest_blob = artifacts.blob_at(request.executor, git_directory, latest_commit, relative_path)
    now = datetime.now(timezone.utc).isoformat()
    requested_surface_id = surface.get("surface_id") or ""
    with sqlite_write_lock():
        session = state._session_factory()
        try:
            # The agent supplies a stable surface id (derived from the target), so a repeat
            # open of the same file/URL reuses one tab; key purely on that id.
            surface_id = requested_surface_id or f"artifact-{uuid.uuid4().hex[:16]}"
            existing = session.get(ArtifactSurfaceRecord, surface_id)
            if existing is None:
                existing = ArtifactSurfaceRecord(id=surface_id, context_id=request.context_id, created_at=now)
                session.add(existing)
            existing.location_uri = request.location_uri
            existing.git_directory = git_directory
            existing.work_tree = work_tree
            existing.relative_path = relative_path
            existing.absolute_path = absolute_path
            existing.kind = surface.get("kind", "image")
            existing.title = surface.get("title", "")
            existing.source = surface.get("source", "")
            existing.tool_call_id = request.tool_call_id
            existing.latest_commit_sha = latest_commit
            existing.latest_blob_sha = latest_blob
            existing.updated_at = now
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico", ".avif"}


def _kind_for_path(relative_path: str) -> str:
    suffix = posixpath.splitext(relative_path)[1].lower()
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in (".html", ".htm", ".xhtml"):
        return "html"
    return "file"


def _annotation_counts_by_version(database_session, context_id: str) -> dict[str, int]:
    """Pin counts keyed by version id (git commit sha)."""
    counts: dict[str, int] = {}
    for row in (
        database_session.query(ArtifactAnnotationRecord)
        .filter(ArtifactAnnotationRecord.context_id == context_id)
        .all()
    ):
        try:
            pins = json.loads(cast(str, row.annotations) or "[]")
        except json.JSONDecodeError:
            pins = []
        counts[cast(str, row.version_id)] = len(pins) if isinstance(pins, list) else 0
    return counts


def _artifact_index(context_id: str, scope: str = "session") -> list[dict]:
    """The file-history list: one entry per tracked ``(git_directory, relative_path)`` with its latest
    change and version count. ``scope='full'`` widens each file to its whole cross-session
    lineage (same location + relative_path), ``'session'`` shows only this session's versions."""
    if state._session_factory is None:
        return []
    database_session = state._session_factory()
    try:
        keys = {
            (cast(str, row.git_directory), cast(str, row.relative_path))
            for row in database_session.query(ArtifactFileRecord.git_directory, ArtifactFileRecord.relative_path)
            .filter(ArtifactFileRecord.context_id == context_id)
            .distinct()
        }
        surfaces = {
            (cast(str, surface.git_directory), cast(str, surface.relative_path)): surface
            for surface in database_session.query(ArtifactSurfaceRecord)
            .filter(ArtifactSurfaceRecord.context_id == context_id)
            .all()
            if surface.relative_path
        }
        items: list[dict] = []
        for git_directory, relative_path in keys:
            query = database_session.query(ArtifactFileRecord).filter(
                ArtifactFileRecord.git_directory == git_directory, ArtifactFileRecord.relative_path == relative_path
            )
            if scope != "full":
                query = query.filter(ArtifactFileRecord.context_id == context_id)
            rows = query.order_by(ArtifactFileRecord.created_at.asc()).all()
            if not rows:
                continue
            latest = rows[-1]
            surface = surfaces.get((git_directory, relative_path))
            items.append({
                "gitDirectory": git_directory,
                "relativePath": relative_path,
                "absolutePath": cast(str, latest.absolute_path),
                "locationUri": cast(str, latest.location_uri),
                "workTree": cast(str, latest.work_tree),
                "versionCount": len(rows),
                "latestCommit": cast(str, latest.commit_sha),
                "latestBlob": cast(str, latest.blob_sha),
                "latestChange": cast(str, latest.change_type),
                "size": cast(int, latest.size),
                "isPlaceholder": bool(latest.is_placeholder),
                "updatedAt": cast(str, latest.created_at),
                "surfaced": surface is not None,
                "kind": cast(str, surface.kind) if surface is not None else _kind_for_path(relative_path),
                "artifactId": cast(str, surface.id) if surface is not None else "",
                "title": (cast(str, surface.title) if surface is not None else "") or posixpath.basename(relative_path),
            })
        items.sort(key=lambda item: item["updatedAt"], reverse=True)
        return items
    finally:
        database_session.close()


def _artifact_versions(context_id: str, git_directory: str, relative_path: str, scope: str = "session") -> list[dict]:
    """Every captured version of one file, oldest → newest (what the filmstrip walks)."""
    if state._session_factory is None:
        return []
    database_session = state._session_factory()
    try:
        query = database_session.query(ArtifactFileRecord).filter(
            ArtifactFileRecord.git_directory == git_directory, ArtifactFileRecord.relative_path == relative_path
        )
        if scope != "full":
            query = query.filter(ArtifactFileRecord.context_id == context_id)
        rows = query.order_by(ArtifactFileRecord.created_at.asc()).all()
        version_ids = [cast(str, row.version_id) for row in rows]
        versions = {
            cast(str, version.id): version
            for version in database_session.query(ArtifactVersionRecord)
            .filter(ArtifactVersionRecord.id.in_(version_ids))
            .all()
        } if version_ids else {}
        annotation_counts = _annotation_counts_by_version(database_session, context_id)
        payload: list[dict] = []
        for row in rows:
            version = versions.get(cast(str, row.version_id))
            commit_sha = cast(str, row.commit_sha)
            payload.append({
                "versionId": commit_sha,  # the UI identity for a version is the commit sha
                "commitSha": commit_sha,
                "blobSha": cast(str, row.blob_sha),
                "sequence": cast(int, version.sequence) if version is not None else 0,
                "changeType": cast(str, row.change_type),
                "size": cast(int, row.size),
                "isPlaceholder": bool(row.is_placeholder),
                "createdAt": cast(str, row.created_at),
                "message": cast(str, version.message) if version is not None else "",
                "toolCallId": cast(str, version.tool_call_id) if version is not None else "",
                "gitDirectory": git_directory,
                "relativePath": relative_path,
                "locationUri": cast(str, row.location_uri),
                "workTree": cast(str, row.work_tree),
                "annotationCount": annotation_counts.get(commit_sha, 0),
            })
        payload.sort(key=lambda item: (item["sequence"], item["createdAt"]))
        return payload
    finally:
        database_session.close()


def _surface_records(context_id: str) -> list[dict]:
    """The surfaced artifacts (artifacts-panel tabs) for a session."""
    if state._session_factory is None:
        return []
    database_session = state._session_factory()
    try:
        rows = (
            database_session.query(ArtifactSurfaceRecord)
            .filter(ArtifactSurfaceRecord.context_id == context_id)
            .order_by(ArtifactSurfaceRecord.created_at.asc())
            .all()
        )
        return [{
            "artifactId": cast(str, row.id),
            "kind": cast(str, row.kind) or "image",
            "title": cast(str, row.title) or posixpath.basename(cast(str, row.relative_path) or cast(str, row.source)),
            "source": cast(str, row.source),
            "gitDirectory": cast(str, row.git_directory),
            "workTree": cast(str, row.work_tree),
            "relativePath": cast(str, row.relative_path),
            "absolutePath": cast(str, row.absolute_path),
            "locationUri": cast(str, row.location_uri),
            "latestCommit": cast(str, row.latest_commit_sha),
            "latestBlob": cast(str, row.latest_blob_sha),
            "toolCallId": cast(str, row.tool_call_id),
            "createdAt": cast(str, row.created_at),
            "updatedAt": cast(str, row.updated_at),
        } for row in rows]
    finally:
        database_session.close()


def _executor_for_location_uri(context_id: str, location_uri: str) -> "LocationExecutor | None":
    """Resolve the executor for one of a session's locations by URI (for serve/restore),
    rebuilding it from the session's location records so it works after a restart."""
    for entry in (_resolve_session_locations(context_id) or []):
        if entry.get("uri") == location_uri:
            address = LocationAddress(
                kind=entry.get("kind", "local"),
                base_directory=entry.get("base_directory", ""),
                host_alias=entry.get("host_alias", ""),
            )
            return executor_for(address)
    if not location_uri or location_uri.startswith("file://"):
        return executor_for(LocationAddress(kind="local", base_directory="", host_alias=""))
    return None


def _restore_artifact(context_id: str, location_uri: str, git_directory: str, work_tree: str, relative_path: str, commit_sha: str) -> None:
    """Restore ``relative_path`` to ``commit_sha`` (append-only), then re-index the new versions."""
    executor = _executor_for_location_uri(context_id, location_uri)
    if executor is None:
        raise HTTPException(status_code=404, detail="Location is unavailable for restore.")
    maximum_bytes = _artifact_maximum_bytes()
    project_id = _project_id_for_context(context_id)
    versions = artifacts.restore(
        executor, git_directory, work_tree, context_id, relative_path, commit_sha,
        maximum_bytes=maximum_bytes,
    )
    request = _CaptureRequest(
        context_id=context_id, location_uri=location_uri, executor=executor,
        base_directory=work_tree, changed_absolute_paths=[posixpath.join(work_tree, relative_path)],
        message=f"restore {relative_path}",
    )
    for version in versions:
        if version.files:
            _record_capture(request, project_id, git_directory, work_tree, version)
    _publish_broadcast({"type": "artifact_captured", "session_id": context_id})


def _artifact_annotation_payload(row: ArtifactAnnotationRecord, surface: "ArtifactSurfaceRecord | None" = None) -> dict:
    """Annotation record → the ``image`` identity the panel renders pins from. The identity
    is ``(surface_id, version_id=commit sha)``; ``source`` is the live file path (readable
    for the latest version's stamping / read_file)."""
    try:
        annotations = json.loads(cast(str, row.annotations) or "[]")
    except json.JSONDecodeError:
        annotations = []
    surface_id = cast(str, row.surface_id)
    version_id = cast(str, row.version_id)
    title = "Image artifact"
    name = ""
    source = ""
    if surface is not None:
        title = cast(str, surface.title) or title
        name = posixpath.basename(cast(str, surface.relative_path) or "")
        source = cast(str, surface.absolute_path) or ""
    return {
        "image": {
            "key": f"{surface_id}::{version_id}",
            "artifactId": surface_id,
            "versionId": version_id,
            "title": title,
            "name": name,
            "versionSeq": 0,
            "source": source,
        },
        "annotations": annotations if isinstance(annotations, list) else [],
        "updatedAt": cast(str, row.updated_at),
    }


def _artifact_annotation_records(context_id: str) -> list[dict]:
    if state._session_factory is None:
        return []
    database_session = state._session_factory()
    try:
        rows = (
            database_session.query(ArtifactAnnotationRecord)
            .filter(ArtifactAnnotationRecord.context_id == context_id)
            .order_by(ArtifactAnnotationRecord.updated_at.desc())
            .all()
        )
        surfaces = {
            cast(str, surface.id): surface
            for surface in database_session.query(ArtifactSurfaceRecord)
            .filter(ArtifactSurfaceRecord.context_id == context_id)
            .all()
        }
        return [_artifact_annotation_payload(row, surfaces.get(cast(str, row.surface_id))) for row in rows]
    finally:
        database_session.close()


def _save_artifact_annotation_record(context_id: str, request: "ArtifactAnnotationSaveRequest") -> dict:
    if state._session_factory is None:
        raise HTTPException(status_code=503, detail="Database is not ready.")
    surface_id = (request.surface_id or "").strip()
    version_id = (request.version_id or "").strip()
    if not surface_id or not version_id:
        raise HTTPException(status_code=400, detail="Annotation surface_id and version_id are required.")
    updated_at = request.updated_at or datetime.now(timezone.utc).isoformat()
    key = {"context_id": context_id, "surface_id": surface_id, "version_id": version_id}
    with sqlite_write_lock():
        database_session = state._session_factory()
        try:
            if database_session.get(SessionRecord, context_id) is None:
                raise HTTPException(status_code=404, detail="Session not found.")
            if not request.annotations:
                row = database_session.get(ArtifactAnnotationRecord, key)
                if row is not None:
                    database_session.delete(row)
                database_session.commit()
                return {"deleted": True}
            row = ArtifactAnnotationRecord(
                context_id=context_id,
                surface_id=surface_id,
                version_id=version_id,
                annotations=json.dumps(request.annotations),
                updated_at=updated_at,
            )
            database_session.merge(row)
            database_session.commit()
            surface = database_session.get(ArtifactSurfaceRecord, surface_id)
            return _artifact_annotation_payload(row, surface)
        except HTTPException:
            database_session.rollback()
            raise
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


def _delete_artifact_annotation_record(context_id: str, surface_id: str, version_id: str) -> bool:
    if state._session_factory is None:
        return False
    key = {"context_id": context_id, "surface_id": surface_id, "version_id": version_id}
    with sqlite_write_lock():
        database_session = state._session_factory()
        try:
            row = database_session.get(ArtifactAnnotationRecord, key)
            if row is None:
                return False
            database_session.delete(row)
            database_session.commit()
            return True
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()





# Loads the title prompt from the shared prompts directory next to the
# harness.core package, mirroring how AgentRuntime resolves its prompt loader.
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
    assert state._global_configuration is not None
    configuration = state._global_configuration
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
        state._broadcaster.publish({"type": "sessions_changed"})


def _set_session_title(context_id: str, title: str) -> bool:
    assert state._session_factory is not None
    with sqlite_write_lock():
        database_session = state._session_factory()
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
    assert state._session_factory is not None
    database_session = state._session_factory()
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
                        state._file_lease_manager.active_for_session(row.id)
                        if state._file_lease_manager is not None
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


def _card_for(agent_name: str, working_directory: str = ""):
    """Build an agent's AgentCard from its config and the skills available to it.

    When a ``working_directory`` is given, skills are scoped to that path (home
    globals plus the path's own ``.agents``, deduped) rather than the server's
    launch directory — so a card advertises the skills a session in that folder
    can actually find. Without one, the server-CWD scoping is used (startup mount)."""
    assert state._global_configuration is not None
    configuration = load_agent_configuration(agent_name, state._global_configuration.agent_directories())
    skill_roots = (
        state._global_configuration.skill_directories_for(working_directory)
        if working_directory
        else state._global_configuration.skill_directories()
    )
    all_skills = load_skills(skill_roots)
    agent_skills = skills_for_agent(all_skills, configuration.skills)
    security_schemes, security = state._global_configuration.a2a.card_security()
    return configuration, build_agent_card(
        configuration, agent_skills, PUBLIC_BASE_URL,
        security_schemes=security_schemes, security=security,
    )


def _mount_agent(application: FastAPI, agent_name: str) -> None:
    """Serve one agent profile as its own A2A endpoint: its own executor, request
    handler, and AgentCard, mounted at a per-agent path. Idempotent."""
    assert state._global_configuration is not None and state._task_store is not None and state._registry is not None
    _configuration, card = _card_for(agent_name)
    if agent_name in state._mounted_agents:
        state._registry.register(agent_name, state._registry._handlers[agent_name], card)
        return
    executor = HarnessAgentExecutor(
        agent_name=agent_name,
        global_configuration=state._global_configuration,
        task_store=state._task_store,
        registry=state._registry,
        on_new_context=_record_session_visible,
        conversations=state._conversations,
        claim_work_habits_acknowledgement=_claim_work_habits_acknowledgement,
        on_turn_state=_set_turn_state,
        on_permission_state=_notify_permission_state,
        session_permission_mode_for=_session_permission_mode_for,
        on_stream_event=_publish_stream_event,
        file_lease_manager=state._file_lease_manager,
        ensure_session_workspace=_ensure_session_workspace,
        ensure_mcp_servers=_ensure_mcp_servers_for,
        resolve_locations=_resolve_session_locations,
        capture_artifacts=_capture_artifacts,
        persist_agent_allow_patterns=_persist_agent_allow_patterns,
    )
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=state._task_store,
        push_config_store=state._push_configuration_store,
        push_sender=state._push_sender,
    )
    state._executors[agent_name] = executor
    state._registry.register(agent_name, handler, card)
    rpc_path = agent_rpc_path(agent_name)
    A2AFastAPIApplication(agent_card=card, http_handler=handler).add_routes_to_app(
        application,
        rpc_url=rpc_path,
        agent_card_url=f"{rpc_path}{AGENT_CARD_PATH}",
    )
    state._mounted_agents.add(agent_name)


def _ensure_agents_for(working_directory: str) -> None:
    """Mount any agent the working directory declares that isn't mounted yet, so a
    folder's project-local agents become addressable A2A routes once that folder is
    selected. The route pool is shared and only grows — nothing is unmounted."""
    assert state._global_configuration is not None
    directories = (
        state._global_configuration.agent_directories_for(working_directory)
        if working_directory
        else state._global_configuration.agent_directories()
    )
    for agent_name in list_agent_route_names(directories):
        if agent_name not in state._mounted_agents:
            _mount_agent(app, agent_name)


def _agent_directories_for_request(working_directory: str) -> list[Path]:
    assert state._global_configuration is not None
    return (
        state._global_configuration.agent_directories_for(working_directory)
        if working_directory
        else state._global_configuration.agent_directories()
    )


def _agent_configuration_for_request(agent_name: str, working_directory: str) -> tuple[Path, _configuration.AgentConfiguration]:
    directories = _agent_directories_for_request(working_directory)
    path = agent_configuration_path(agent_name, directories)
    return path, load_agent_configuration(agent_name, directories)


def _agent_configuration_payload(agent_name: str, working_directory: str) -> "AgentConfigurationResponse":
    path, configuration = _agent_configuration_for_request(agent_name, working_directory)
    return AgentConfigurationResponse(
        id=configuration.identifier,
        name=configuration.name,
        title=configuration.display_name,
        model=configuration.model or "",
        provider=configuration.provider or "",
        reasoning_effort=configuration.reasoning_effort,
        permission_mode=configuration.permission_mode,
        stream_agent_progress=configuration.stream_agent_progress,
        tools_enabled=configuration.tools_enabled,
        bash=AgentBashConfigurationResponse(
            enabled=configuration.tools.bash.enabled,
            background_allowed=configuration.tools.bash.background_allowed,
            permissions=dict(configuration.tools.bash.permissions),
        ),
        spawn_agent=AgentSpawnConfigurationResponse(enabled=configuration.tools.spawn_agent.enabled),
        path=str(path.with_name("configuration.json")),
    )


def _load_agent_sidecar(agent_markdown_path: Path) -> dict[str, Any]:
    sidecar_path = agent_markdown_path.with_name("configuration.json")
    if not sidecar_path.exists():
        return {}
    data = json.loads(sidecar_path.read_text())
    return data if isinstance(data, dict) else {}


def _save_agent_sidecar(agent_markdown_path: Path, data: dict[str, Any]) -> None:
    sidecar_path = agent_markdown_path.with_name("configuration.json")
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _apply_agent_configuration_update(sidecar: dict[str, Any], request: "AgentConfigurationUpdateRequest") -> dict[str, Any]:
    model = AgentSidecar.from_mapping(sidecar)
    if request.model is not None or request.provider is not None or request.reasoning_effort is not None:
        model.set_preset(
            model=request.model if request.model is not None else ...,
            provider=request.provider if request.provider is not None else ...,
            reasoning_effort=request.reasoning_effort if request.reasoning_effort is not None else ...,
        )
    if request.permission_mode is not None:
        model.permission_mode = request.permission_mode
    if request.stream_agent_progress is not None:
        model.stream_agent_progress = request.stream_agent_progress
    if request.tools_enabled is not None:
        model.set_tools_enabled(request.tools_enabled)
    if request.bash is not None:
        model.set_bash(
            enabled=request.bash.enabled if request.bash.enabled is not None else ...,
            background_allowed=request.bash.background_allowed if request.bash.background_allowed is not None else ...,
            permissions=(
                AgentSidecar.normalized_permissions(request.bash.permissions)
                if request.bash.permissions is not None
                else ...
            ),
        )
    if request.spawn_agent is not None:
        model.set_spawn_agent(
            enabled=request.spawn_agent.enabled if request.spawn_agent.enabled is not None else ...,
        )
    return model.to_mapping()


async def _persist_agent_allow_patterns(agent_identifier: str, project_directory: str, patterns: list[str]) -> None:
    """Durably add allow-patterns to a delegated agent profile's configured bash permissions,
    so a delegated agent's 'always allow' outlives its ephemeral runtime and every future spawn
    of the profile inherits it. Best effort; an existing decision for a pattern is never
    overridden (a deliberate deny/ask is not silently flipped to allow)."""
    if not patterns or state._global_configuration is None:
        return

    def _write() -> bool:
        try:
            if project_directory:
                _ensure_agents_for(project_directory)
            agent_markdown_path, _configuration = _agent_configuration_for_request(agent_identifier, project_directory)
        except FileNotFoundError:
            return False
        sidecar = AgentSidecar.from_mapping(_load_agent_sidecar(agent_markdown_path))
        if not sidecar.grant_bash_patterns(patterns):
            return False
        _save_agent_sidecar(agent_markdown_path, sidecar.to_mapping())
        return True

    if not await asyncio.to_thread(_write):
        return
    # The current delegated agent already runs the command (its live session allowlist covers
    # it); this makes the change take effect for future spawns and the discovery cards.
    if agent_identifier in state._executors:
        state._executors[agent_identifier].reset_runtimes()
    await asyncio.to_thread(_reload_agent_cards)
    _publish_broadcast({"type": "agents_changed"})


def _reload_agent_cards() -> None:
    """Recompile AgentCards from the agent markdown and skill files so discovery
    reflects edits without a restart. Agent behaviour itself is already live,
    since each turn loads its configuration and skills fresh."""
    assert state._global_configuration is not None and state._registry is not None
    for agent_name in list_agent_route_names(state._global_configuration.agent_directories()):
        handler = state._registry._handlers.get(agent_name)
        if handler is not None:
            _configuration, card = _card_for(agent_name)
            state._registry.register(agent_name, handler, card)


def _watched_a2a_paths() -> list[str]:
    assert state._global_configuration is not None
    directories = [
        # The .agents roots are watched recursively so mcp.json (live MCP server
        # definitions) is picked up alongside the agents/ and skills/ subtrees.
        *state._global_configuration.agents_root_directories(),
        *state._global_configuration.agent_directories(),
        *state._global_configuration.skill_directories(),
    ]
    watched: list[str] = []
    seen: set[Path] = set()
    for directory in directories:
        if not directory.is_dir():
            continue
        key = directory.resolve()
        if key in seen:
            continue
        seen.add(key)
        watched.append(str(key))
    return watched


async def _reload_mcp() -> None:
    """Re-read mcp.json and apply the server set live: reconcile the client manager
    (start new servers, stop removed/disabled ones, keep unchanged ones connected)
    and drop cached runtimes so the next turn rebuilds its tools with the new set.
    No server restart required."""
    assert state._global_configuration is not None
    # Serialize with the settings endpoints and the configuration watcher: they all
    # rebuild the shared `_mcp_manager`, so overlapping runs would clobber it.
    async with state._configuration_lock:
        state._global_configuration.mcp = GlobalConfiguration.load().mcp
        # Re-fold the startup-provisioned Composio server back in so a live mcp.json
        # edit doesn't drop Composio's tools (and the agent keeps its MCP tools).
        state._global_configuration.mcp.servers.update(state._composio_servers)
        enabled = state._global_configuration.mcp.enabled_servers()
        if state._mcp_manager is None:
            if enabled:
                state._mcp_manager = MCPClientManager(enabled)
                await state._mcp_manager.start()
                set_mcp_client_manager(state._mcp_manager)
        else:
            await state._mcp_manager.reconcile(enabled)
        for executor in state._executors.values():
            executor.reset_runtimes()


def _configure_telemetry(configuration: GlobalConfiguration) -> None:
    telemetry_configuration = configuration.telemetry
    _telemetry.configure(
        enabled=telemetry_configuration.enabled,
        endpoint=telemetry_configuration.exporter.endpoint,
        headers=telemetry_configuration.resolved_headers(),
        sample_ratio=telemetry_configuration.sample_ratio,
    )


def _remote_agent_dataclasses() -> dict[str, RemoteAgentConfiguration]:
    """Convert the loaded ``remote-agents.json`` config into the manager's dataclasses."""
    assert state._global_configuration is not None
    result: dict[str, RemoteAgentConfiguration] = {}
    for name, configuration in state._global_configuration.remote_agents.enabled_agents().items():
        auth = configuration.auth
        result[name] = RemoteAgentConfiguration(
            name=name,
            card_url=configuration.card_url,
            auth=RemoteAgentAuth(
                kind=auth.type, token=auth.token, header=auth.header, scheme_prefix=auth.scheme_prefix,
                token_url=auth.token_url, client_id=auth.client_id, client_secret=auth.client_secret,
                scopes=list(auth.scopes),
            ),
            card_ttl_seconds=configuration.card_ttl_seconds,
            allowed_hosts=list(configuration.allowed_hosts),
            allow_private=configuration.allow_private,
            allowed_profiles=list(configuration.allowed_profiles),
        )
    return result


async def _reload_remote_agents() -> None:
    """Re-read remote-agents.json and apply the external-agent set live: reconcile the
    outbound client manager and drop cached runtimes so the next turn's roster reflects
    the change. No server restart required."""
    assert state._global_configuration is not None and state._registry is not None
    async with state._configuration_lock:
        state._global_configuration.remote_agents = GlobalConfiguration.load().remote_agents
        configurations = _remote_agent_dataclasses()
        if state._remote_agent_manager is None:
            state._remote_agent_manager = RemoteAgentManager(configurations)
            await state._remote_agent_manager.start()
            state._registry.set_remote_manager(state._remote_agent_manager)
        else:
            await state._remote_agent_manager.reconcile(configurations)
        for executor in state._executors.values():
            executor.reset_runtimes()
        state._broadcaster.publish({"type": "remote_agents_changed"})


async def _poll_remote_agent_health(interval_seconds: float = 300.0) -> None:
    """Periodically re-resolve remote agent cards so their health stays current in the UI
    even while idle, broadcasting on each pass so open panels refresh."""
    while True:
        await asyncio.sleep(interval_seconds)
        if state._remote_agent_manager is not None and state._remote_agent_manager.has_agents():
            await state._remote_agent_manager.refresh_all()
            _publish_broadcast({"type": "remote_agents_changed"})


async def _ensure_mcp_servers_for(working_directory: str) -> None:
    """Additively grow the shared MCP server pool with the working directory's own
    ``mcp.json`` servers, so a folder's servers are running and listable once that
    folder is selected. The pool is a union — servers are only added or updated,
    never removed — so no other session loses its servers."""
    assert state._global_configuration is not None
    if not working_directory:
        return
    # Serialize with every other `_mcp_manager` mutator (settings save, config/mcp.json
    # watchers) so concurrent reconciles never clobber the shared manager.
    async with state._configuration_lock:
        folder_servers = state._global_configuration.mcp_configuration_for(working_directory).servers
        new_servers = {
            name: configuration
            for name, configuration in folder_servers.items()
            if state._global_configuration.mcp.servers.get(name) != configuration
        }
        if not new_servers:
            return
        state._global_configuration.mcp.servers.update(new_servers)
        enabled = state._global_configuration.mcp.enabled_servers()
        if state._mcp_manager is None:
            if enabled:
                state._mcp_manager = MCPClientManager(enabled)
                await state._mcp_manager.start()
                set_mcp_client_manager(state._mcp_manager)
        else:
            await state._mcp_manager.reconcile(enabled)
        for executor in state._executors.values():
            executor.reset_runtimes()


async def _watch_agents_and_skills(application: FastAPI) -> None:
    """Watch the agents, skills, and mcp.json sources; on any change, mount newly
    added agents, reload MCP servers, refresh cards, and broadcast so connected
    clients refetch immediately. Agents, skills, and MCP servers are all picked up
    live, so the only thing needing a restart is a change to the core harness."""
    assert state._global_configuration is not None
    watched = _watched_a2a_paths()
    if not watched:
        return
    try:
        async for changes in awatch(*watched):
            if any(str(path).endswith("mcp.json") for _change, path in changes):
                await _reload_mcp()
            if any(str(path).endswith("remote-agents.json") for _change, path in changes):
                await _reload_remote_agents()
            for agent_name in list_agent_route_names(state._global_configuration.agent_directories()):
                if agent_name not in state._mounted_agents:
                    _mount_agent(application, agent_name)
            _reload_agent_cards()
            state._broadcaster.publish({"type": "agents_changed"})
    except asyncio.CancelledError:
        pass


def _rebuild_web_fetch_clients(configuration: "GlobalConfiguration") -> None:
    """Point the web-fetch tool's engines at the current config: the Jina Reader key
    (default engine; empty means keyless), a live Firecrawl client when a key is present
    (else none), and the optional proxy for the direct/download tiers."""
    set_jina_api_key(configuration.jina.effective_api_key)
    set_proxy_url(configuration.web_fetch.effective_proxy_url)
    firecrawl_key = configuration.firecrawl.effective_api_key
    if firecrawl_key:
        from firecrawl import AsyncFirecrawl
        api_url = configuration.firecrawl.effective_api_url
        set_firecrawl_client(
            AsyncFirecrawl(api_key=firecrawl_key, api_url=api_url)
            if api_url
            else AsyncFirecrawl(api_key=firecrawl_key)
        )
    else:
        set_firecrawl_client(None)


async def _apply_live_credentials() -> None:
    """Rebuild every credential-dependent live client from the current in-memory
    configuration: the Exa client, the web-fetch (Jina/Firecrawl) engines, the
    Composio/MCP server set and its client manager, and drop cached agent runtimes so
    the next turn is built with the new credentials. Shared by the settings endpoint
    and the on-disk configuration watcher."""
    assert state._global_configuration is not None
    configuration = state._global_configuration
    # Push the tool tuning policy process-wide so every window-scaled cap, timeout, and settlement
    # knob tracks the current configuration (mirrors the Exa/web-fetch client rebuild below).
    set_tuning(tuning_from_policy(configuration.tuning))
    exa_key = configuration.exa.effective_api_key
    if exa_key:
        from exa_py import Exa
        set_exa_client(Exa(api_key=exa_key))
    else:
        set_exa_client(None)
    _rebuild_web_fetch_clients(configuration)
    # Re-provision Composio: rebuild its server config, fold it into (or remove it from)
    # the MCP set, and restart the client manager so Composio tools track its key.
    state._composio_servers = composio_mcp_servers(configuration.composio)
    if state._composio_servers:
        configuration.mcp.servers.update(state._composio_servers)
    else:
        configuration.mcp.servers.pop(configuration.composio.server_name, None)
    if state._mcp_manager is not None:
        await state._mcp_manager.aclose()
    mcp_servers = configuration.mcp.enabled_servers()
    state._mcp_manager = MCPClientManager(mcp_servers) if mcp_servers else None
    if state._mcp_manager is not None:
        await state._mcp_manager.start()
    set_mcp_client_manager(state._mcp_manager)
    for executor in state._executors.values():
        executor.reset_runtimes()


# The content digest of the last configuration.yaml this process wrote, so the on-disk
# watcher can tell our own saves (skip — already applied in-process) apart from a manual
# user edit (apply). Set by every server-side configuration write via _persist_configuration.

# Serializes every configuration mutation-and-apply (settings endpoints + the on-disk
# watcher) so they never interleave at an await point. Without it, a UI save and a
# concurrent disk-edit reload could both rebuild the MCP client manager, clobbering the
# `_mcp_manager` global and leaking a half-started manager.

# The in-flight ChatGPT sign-in, if any. A sign-in owns a loopback server on port
# 1455 for its lifetime, so at most one runs at a time; starting a new one supersedes
# any stale flow. Held only between /auth/chatgpt/start and the browser redirect.


def _configuration_digest() -> Optional[str]:
    """A content hash of ~/.daisy/configuration.yaml, or ``None`` if it is absent."""
    try:
        return hashlib.sha256(configuration_file_path().read_bytes()).hexdigest()
    except OSError:
        return None


async def _persist_configuration(**changes) -> None:
    """Write configuration changes to disk and remember the resulting content digest so
    the on-disk watcher does not treat our own save as an external edit."""
    await asyncio.to_thread(save_api_keys, **changes)
    state._last_written_configuration_digest = await asyncio.to_thread(_configuration_digest)


async def _reload_configuration_from_disk() -> None:
    """Re-read ~/.daisy/configuration.yaml after a manual on-disk edit and apply it live:
    refresh the in-memory credentials/settings, rebuild the credential-dependent clients,
    and broadcast so every connected client refetches. The MCP server *set* (mcp.json plus
    any folder-added servers) is left to its own watcher — only the credential-derived
    Composio server is re-provisioned here."""
    assert state._global_configuration is not None
    fresh = await asyncio.to_thread(GlobalConfiguration.load)
    configuration = state._global_configuration
    user_context_setting_changed = configuration.user_context.enabled != fresh.user_context.enabled
    configuration.exa = fresh.exa
    configuration.jina = fresh.jina
    configuration.firecrawl = fresh.firecrawl
    configuration.web_fetch = fresh.web_fetch
    configuration.composio = fresh.composio
    configuration.sandbox = fresh.sandbox
    configuration.workspace = fresh.workspace
    configuration.compaction = fresh.compaction
    configuration.user_context = fresh.user_context
    configuration.computer_control = fresh.computer_control
    configuration.tuning = fresh.tuning
    configuration.providers = fresh.providers
    configuration.default_agent = fresh.default_agent
    await _apply_live_credentials()
    if user_context_setting_changed:
        await asyncio.to_thread(_reset_work_habits_acknowledgements)
    state._broadcaster.publish({"type": "settings_changed"})


async def _watch_configuration() -> None:
    """Watch ~/.daisy/configuration.yaml and mirror manual on-disk edits into the running
    server and every connected client — so editing an API key (or any setting) directly in
    the file takes effect immediately, no restart. The file is the single source of truth;
    our own writes are recognized by digest and skipped so a UI save does not echo."""
    configuration_path = configuration_file_path()
    filename = configuration_path.name
    try:
        async for _changes in awatch(
            str(configuration_path.parent),
            recursive=False,
            watch_filter=lambda _change, path: Path(path).name == filename,
        ):
            # Serialize against UI-driven saves. Re-check the digest *inside* the lock so a
            # save that completed while we waited is recognized as ours (skip), not echoed.
            async with state._configuration_lock:
                digest = await asyncio.to_thread(_configuration_digest)
                if digest is not None and digest == state._last_written_configuration_digest:
                    continue  # our own write echoing back — already applied in-process
                state._last_written_configuration_digest = digest
                await _reload_configuration_from_disk()
    except asyncio.CancelledError:
        pass


async def _watch_ssh_hosts() -> None:
    """Watch ~/.ssh/config and broadcast when the SSH host registry changes, so the UI's
    host dropdowns and location status refresh the moment a host is added/edited on disk.
    ~/.ssh also holds keys and known_hosts (which churn), so we filter to just ``config``."""
    ssh_config_path = Path("~/.ssh/config").expanduser()
    directory = str(ssh_config_path.parent)
    if not ssh_config_path.parent.exists():
        return
    try:
        async for _changes in awatch(
            directory,
            recursive=False,
            watch_filter=lambda _change, path: Path(path).name == "config",
        ):
            state._broadcaster.publish({"type": "hosts_changed"})
    except asyncio.CancelledError:
        pass


@asynccontextmanager
async def lifespan(application: FastAPI):
    state._main_loop = asyncio.get_running_loop()
    state._file_lease_manager = FileLeaseManager(on_change=_notify_filesystem_lease_state)
    state._workspace_manager = SessionWorkspaceManager()
    state._terminal_manager = TerminalSessionManager()
    state._global_configuration = GlobalConfiguration.load()
    _assert_exposure_authenticated(state._global_configuration)
    _configure_telemetry(state._global_configuration)
    # Seed the home layer (~/.agents) with editable copies of the server-shipped
    # agents/skills, non-destructively. This is what makes the desktop app's bundled
    # profiles appear AND gives each a writable home copy so per-agent settings (the
    # model choice) can persist — the bundled base is read-only inside the frozen app.
    seeded = await asyncio.to_thread(seed_home_agents)
    if seeded:
        logging.getLogger("harness.server").info("seeded home agents/skills: %s", ", ".join(seeded))
    # Seed the digest with the just-loaded file (GlobalConfiguration.load may create it
    # from the packaged template) so that first bootstrap write is not mistaken for a
    # manual edit by the configuration watcher.
    state._last_written_configuration_digest = _configuration_digest()

    database_path = database_file_path()
    configure_sqlite_lock(database_path)
    sync_engine = create_engine(f"sqlite:///{database_path}")

    # SQLite concurrency: WAL lets readers run alongside the single writer (the
    # task store writes on every streamed event, while the UI reads sessions /
    # projects), and a busy timeout makes a contended op wait instead of raising
    # "database is locked". Register BEFORE create_all so the first pooled
    # connection (from create_all itself) gets the pragmas.
    @event.listens_for(sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    def _initialize_history_schema() -> None:
        with sqlite_write_lock():
            _apply_history_schema(sync_engine)

    # Off the loop, like every other history.db writer: even though startup has no
    # concurrency yet, keeping the invariant absolute ("no coroutine ever acquires the
    # synchronous write lock on the loop thread") is what prevents this whole class of
    # deadlock from creeping back in.
    await asyncio.to_thread(_initialize_history_schema)
    state._session_factory = sessionmaker(bind=sync_engine)
    # Guarantee at least one project exists so the app always opens into a live workspace
    # (no landing page, no empty state). On a fresh install this seeds the "Home" project.
    await asyncio.to_thread(_ensure_default_project)

    # Install the tool tuning policy from the loaded configuration before any tool can run.
    set_tuning(tuning_from_policy(state._global_configuration.tuning))

    exa_key = state._global_configuration.exa.effective_api_key
    if exa_key:
        from exa_py import Exa
        set_exa_client(Exa(api_key=exa_key))
    _rebuild_web_fetch_clients(state._global_configuration)

    # Provision the Composio Tool Router (best-effort) and fold it into the MCP
    # config itself, so both the client manager and the agent's tool gating
    # (which binds list_mcp_tools/call_mcp_tool only when a server is configured)
    # see it — Composio's tools then ride the normal MCP path.
    state._composio_servers = composio_mcp_servers(state._global_configuration.composio)
    state._global_configuration.mcp.servers.update(state._composio_servers)
    mcp_servers = state._global_configuration.mcp.enabled_servers()
    state._mcp_manager = MCPClientManager(mcp_servers) if mcp_servers else None
    set_mcp_client_manager(state._mcp_manager)
    mcp_start_task: asyncio.Task[None] | None = None
    if state._mcp_manager is not None:
        # Connect MCP servers in the BACKGROUND so a slow or hung server (a cold
        # `uvx`/`npx` spawn, a stalled HTTP endpoint) can never delay — let alone block —
        # the harness boot; the app was failing to start when an MCP handshake stalled.
        # The manager is already wired (tool gating keys on it, not on live connections),
        # so each server's tools simply appear as it finishes connecting. The lifespan
        # owns the task and cancels it during teardown.
        mcp_start_task = asyncio.create_task(state._mcp_manager.start())

    state._async_engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        # Wait up to 30s for the write lock instead of raising "database is locked"
        # when the task store and UI reads contend. WAL (set above) lets reads run
        # concurrently with writes.
        connect_args={"timeout": 30},
    )

    @event.listens_for(state._async_engine.sync_engine, "connect")
    def _set_async_sqlite_pragma(dbapi_connection, _connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    state._task_store = AppendOnlyTaskStore(state._async_engine)
    await state._task_store.initialize()
    orphaned_task_ids = await state._task_store.reconcile_orphaned_turns()
    if orphaned_task_ids:
        logging.getLogger("harness.server").warning(
            "Marked %d A2A task(s) interrupted after server restart.",
            len(orphaned_task_ids),
        )
    # An input-required task is preserved (not orphaned): restore its awaiting-input
    # marker so the sidebar shows the pause survived the restart. A later answer resumes
    # it durably from its checkpoint.
    state._awaiting_input_contexts.update(await state._task_store.input_required_context_ids())

    state._registry = AgentRegistry(state._task_store)
    state._file_url_signer = FileUrlSigner(
        load_or_create_secret(harness_home_directory()),
        PUBLIC_BASE_URL,
        allowed_root=harness_home_directory() / "uploads",
    )
    state._registry.set_file_url_signer(state._file_url_signer)
    state._push_configuration_store = PersistentPushNotificationConfigurationStore(state._async_engine)
    await state._push_configuration_store.initialize()
    state._push_httpx_client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    state._push_sender = PinnedPushNotificationSender(
        state._push_httpx_client,
        state._push_configuration_store,
        allow_private=state._push_configuration_store.allow_private_webhooks,
    )
    for agent_name in list_agent_route_names(state._global_configuration.agent_directories()):
        _mount_agent(application, agent_name)

    # Outbound A2A: build the external-agent client manager from remote-agents.json and
    # install it on the registry, so a delegation to a registered remote agent is routed
    # over the wire. Card resolution is best-effort (started in the background) so an
    # unreachable peer never blocks boot.
    _remote_agent_configurations = _remote_agent_dataclasses()
    if _remote_agent_configurations:
        state._remote_agent_manager = RemoteAgentManager(_remote_agent_configurations)
        state._registry.set_remote_manager(state._remote_agent_manager)
        asyncio.create_task(state._remote_agent_manager.start())

    # Kill any process groups orphaned by a previous unclean shutdown (a SIGKILL or
    # crash could not run the teardown handlers) so a background shell subtree — a
    # dev server, a watcher — never survives across a restart. Runs before recovery
    # marks those jobs abandoned.
    await asyncio.to_thread(reap_orphaned_processes)

    # Recover background jobs persisted by a previous run: interrupted ones are
    # flagged for re-run and every context with a deliverable result is woken with
    # an autonomous turn so the agent picks the work back up on its own.
    for executor in state._executors.values():
        await executor.resume_pending_on_startup()

    watcher = asyncio.create_task(_watch_agents_and_skills(application))
    configuration_watcher = asyncio.create_task(_watch_configuration())
    ssh_hosts_watcher = asyncio.create_task(_watch_ssh_hosts())
    remote_agent_health_poller = asyncio.create_task(_poll_remote_agent_health())
    # The artifact-capture worker drains write-ish tool calls and runs the shadow-git
    # capture off-loop, so it never blocks a turn (best-effort, see _run_capture).
    state._capture_queue = asyncio.Queue(maxsize=4096)
    capture_worker = asyncio.create_task(_capture_worker())
    try:
        yield
    finally:
        watcher.cancel()
        configuration_watcher.cancel()
        ssh_hosts_watcher.cancel()
        remote_agent_health_poller.cancel()
        capture_worker.cancel()
        if mcp_start_task is not None and not mcp_start_task.done():
            mcp_start_task.cancel()
            with suppress(asyncio.CancelledError):
                await mcp_start_task
        if state._terminal_manager is not None:
            await state._terminal_manager.close_all()
        cancel_all_background_tasks()
        if state._mcp_manager is not None:
            await state._mcp_manager.aclose()
        if state._proxy_client is not None:
            await state._proxy_client.aclose()


# The only browsers that legitimately call this API are the desktop app's own webview (Tauri,
# whose origin is tauri://localhost / http://tauri.localhost by platform) and the local dev
# server — never an arbitrary internet page. Reflecting `*` let any site the user visited script
# the localhost, tool-executing API; scope CORS to the app's own origins instead.
_APP_ORIGIN_REGEX = r"^(tauri://localhost|https?://tauri\.localhost|https?://localhost(:\d+)?|https?://127\.0\.0\.1(:\d+)?)$"
_app_origin_matcher = re.compile(_APP_ORIGIN_REGEX)

app = FastAPI(title="harness", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=_APP_ORIGIN_REGEX,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# Cache one JWKS client per issuer URL — each fetches and caches signing keys itself.


def _a2a_request_authorized(request: Request, configuration: "_configuration.A2AServerConfiguration") -> bool:
    """Whether an inbound A2A request satisfies the configured auth: a matching API-key
    header, or a Bearer JWT that verifies against the configured JWKS (issuer/audience)."""
    if configuration.api_key:
        provided = request.headers.get(configuration.api_key_header, "")
        if provided and hmac.compare_digest(provided, configuration.api_key):
            return True
    if configuration.oauth2_jwks_url:
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            token = header[len("Bearer "):]
            try:
                client = state._jwks_clients.get(configuration.oauth2_jwks_url)
                if client is None:
                    client = jwt.PyJWKClient(configuration.oauth2_jwks_url)
                    state._jwks_clients[configuration.oauth2_jwks_url] = client
                signing_key = client.get_signing_key_from_jwt(token)
                jwt.decode(
                    token,
                    signing_key.key,
                    algorithms=["RS256", "ES256"],
                    audience=configuration.oauth2_audience or None,
                    issuer=configuration.oauth2_issuer or None,
                )
                return True
            except Exception:
                return False
    return False


@app.middleware("http")
async def _a2a_auth_middleware(request: Request, call_next):
    """Enforce inbound auth on the A2A RPC endpoints when configured. Discovery (the
    well-known card) and self-authenticating signed file URLs stay public so peers can
    still resolve the agent and fetch files it handed them."""
    configuration = state._global_configuration.a2a if state._global_configuration is not None else None
    path = request.url.path
    protected = (
        configuration is not None
        and configuration.enabled()
        and path.startswith("/a2a/")
        and not path.startswith("/a2a/files/")
        and not path.endswith("/.well-known/agent-card.json")
    )
    if protected and not _a2a_request_authorized(request, configuration):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return await call_next(request)


@app.exception_handler(Exception)
async def _cors_exception_handler(request: Request, exc: Exception):
    """Starlette's CORSMiddleware sits inside ServerErrorMiddleware, so exceptions
    that reach the outer middleware bypass CORS entirely — the browser then blocks
    the response and the frontend sees a CORS error instead of the real status.
    Catch unhandled exceptions here and return a CORS-headed 500 so the client at
    least sees the error code."""
    import logging as _logging
    _logging.getLogger("harness.server").exception("Unhandled error in %s %s", request.method, request.url.path)
    # Reflect the request origin only when it is one of the app's own (matching the CORS policy),
    # so the client still sees the error code without opening the response to arbitrary origins.
    origin = request.headers.get("origin", "")
    headers = {"Access-Control-Allow-Origin": origin} if origin and _app_origin_matcher.match(origin) else {}
    return JSONResponse(status_code=500, content={"detail": "Internal server error"}, headers=headers)


















































# Projects, locations, and the SSH host registry.
#
# A project is the internal grouping key for locations (local + SSH remotes) and sessions.
# Locations are the user-facing targets. These are server-owned domain data in history.db.
# Hosts are read from ~/.ssh/config (the OS source of truth) via the system ssh.





def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _location_address(record: "LocationRecord") -> LocationAddress:
    return LocationAddress(kind=record.kind, base_directory=record.base_directory, host_alias=record.host_alias or "")


def _serialize_location(record: "LocationRecord") -> dict[str, Any]:
    """A location for the API: its generated URI (identity), derived name, connection, and
    its one execution policy (permission_mode)."""
    try:
        uri = location_uri_for(_location_address(record))
    except Exception:
        uri = ""
    host_known = record.kind == "local" or (bool(record.host_alias) and host_is_defined(record.host_alias))
    return {
        "id": record.id,
        "project_id": record.project_id,
        "name": record.name,
        "kind": record.kind,
        "host_alias": record.host_alias or "",
        "host_known": host_known,
        "base_directory": record.base_directory,
        "uri": uri,
        "permission_mode": record.permission_mode or "default",
        "created_at": record.created_at,
    }


def _serialize_project(record: "ProjectRecord", database_session, *, with_locations: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": record.id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
    if with_locations:
        locations = (
            database_session.query(LocationRecord)
            .filter(LocationRecord.project_id == record.id)
            .order_by(LocationRecord.created_at.asc())
            .all()
        )
        payload["locations"] = [_serialize_location(location) for location in locations]
    session_count = database_session.query(SessionRecord).filter(SessionRecord.project_id == record.id).count()
    payload["session_count"] = session_count
    return payload


def _derive_location_name(database_session, project_id: str, kind: str, base_directory: str, host_alias: str, *, exclude_id: str = "") -> str:
    """The agent-facing name for a location, derived from its connection (not user-entered):
    the SSH host alias for a remote, the base directory's folder name for a local (falling
    back to "local"/"remote"). Deduplicated within the project with a numeric suffix so two
    locations never collide on the name the agent addresses them by."""
    if kind == "remote":
        base = (host_alias or "").strip() or "remote"
    else:
        base = Path(base_directory.strip().rstrip("/")).name or "local"
    existing = {
        row.name for row in database_session.query(LocationRecord.name)
        .filter(LocationRecord.project_id == project_id, LocationRecord.id != exclude_id).all()
    }
    if base not in existing:
        return base
    index = 2
    while f"{base}-{index}" in existing:
        index += 1
    return f"{base}-{index}"


def _location_pair_conflict(first: tuple[str, str, str], second: tuple[str, str, str]) -> str | None:
    """The overlap message for a single pair of normalized (machine, path, raw) locations,
    or ``None`` if they don't conflict. They conflict only on the same machine, when the two
    directories are identical or one is nested inside the other."""
    (machine_a, path_a, raw_a), (machine_b, path_b, raw_b) = first, second
    if machine_a != machine_b or not path_a or not path_b:
        return None
    if path_a == path_b:
        return f"Two locations use the same directory {raw_a}. Each location must be a distinct place, so remove one or point it somewhere else."
    if path_b.startswith(path_a + "/"):
        return f"{raw_b} is inside {raw_a}, so the two overlap. A location already covers everything beneath it — give each one its own separate directory."
    if path_a.startswith(path_b + "/"):
        return f"{raw_a} is inside {raw_b}, so the two overlap. A location already covers everything beneath it — give each one its own separate directory."
    return None


def _locations_conflict_message(entries: list[tuple[str, str, str]]) -> str | None:
    """A human message for the first pair of locations that overlap on the same machine —
    identical base directories, or one nested inside another — which is redundant and
    ambiguous for the agent to address. ``entries`` is a list of (kind, host_alias,
    base_directory); locations on different machines never conflict, even with the same path."""
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


def _existing_location_entries(database_session, project_id: str, *, exclude_id: str = "") -> list[tuple[str, str, str]]:
    rows = (
        database_session.query(LocationRecord)
        .filter(LocationRecord.project_id == project_id, LocationRecord.id != exclude_id)
        .all()
    )
    return [(row.kind, row.host_alias or "", row.base_directory) for row in rows]


def _add_location_row(database_session, project_id: str, location_input: LocationInput) -> "LocationRecord":
    kind = location_input.kind if location_input.kind in ("local", "remote") else "local"
    host_alias = (location_input.host_alias or "").strip()
    base_directory = location_input.base_directory.strip()
    record = LocationRecord(
        id=str(uuid.uuid4()),
        project_id=project_id,
        name=_derive_location_name(database_session, project_id, kind, base_directory, host_alias),
        kind=kind,
        host_alias=host_alias,
        base_directory=base_directory,
        permission_mode=location_input.permission_mode or "default",
        created_at=_iso_now(),
    )
    database_session.add(record)
    return record


def _projects_payload() -> dict[str, list[dict[str, Any]]]:
    assert state._session_factory is not None
    database_session = state._session_factory()
    try:
        rows = database_session.query(ProjectRecord).order_by(ProjectRecord.updated_at.desc()).all()
        return {"projects": [_serialize_project(row, database_session) for row in rows]}
    finally:
        database_session.close()


def _project_payload(project_id: str) -> dict[str, Any] | None:
    assert state._session_factory is not None
    database_session = state._session_factory()
    try:
        record = database_session.get(ProjectRecord, project_id)
        return _serialize_project(record, database_session) if record is not None else None
    finally:
        database_session.close()


def _create_project(request: ProjectCreateRequest) -> dict[str, Any]:
    assert state._session_factory is not None
    conflict = _locations_conflict_message([(location.kind, location.host_alias, location.base_directory) for location in request.locations])
    if conflict:
        raise ValueError(conflict)
    with sqlite_write_lock():
        database_session = state._session_factory()
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

    The initial location targets the server user's home directory. This is a no-op once any
    project exists, so it never changes user-created groupings.
    """
    assert state._session_factory is not None
    with sqlite_write_lock():
        database_session = state._session_factory()
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
    assert state._session_factory is not None
    database_session = state._session_factory()
    try:
        return database_session.query(ProjectRecord).count()
    finally:
        database_session.close()


def _full_disk_access_granted() -> bool:
    """Whether *this* process can read Full-Disk-Access-protected data, tested by trying to
    read a byte of the user's TCC database (a canonical FDA-gated file). Reflects the reality
    the user-context probe faces: in the packaged app FDA is attributed to Daisy.app (the
    responsible parent of the server), so this flips true once the user grants it. Any
    permission/OS error means no access."""
    protected = Path.home() / "Library" / "Application Support" / "com.apple.TCC" / "TCC.db"
    try:
        with open(protected, "rb") as handle:
            handle.read(1)
        return True
    except OSError:
        return False


def _open_full_disk_access_settings() -> None:
    """Open System Settings straight to the Full Disk Access pane so the user can add Daisy in
    one hop. Best-effort; a non-macOS or failed ``open`` is simply a no-op."""
    with suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"],
            check=False, timeout=5,
        )


def _accessibility_granted() -> bool:
    """Whether this process may read the AX tree and control other apps — the permission
    the computer-use tool needs. In the packaged app it attaches to Daisy.app (the
    responsible parent of the server), like Full Disk Access."""
    try:
        from harness.computer import permissions
        return permissions.accessibility_granted()
    except Exception:
        return False


def _request_accessibility() -> None:
    """Surface the system Accessibility prompt (deep-links to the pane) if not yet trusted."""
    with suppress(Exception):
        from harness.computer import permissions
        permissions.request_accessibility()


def _open_accessibility_settings() -> None:
    with suppress(Exception):
        from harness.computer import permissions
        permissions.open_accessibility_settings()


def _request_screen_recording() -> None:
    """Surface the system Screen Recording prompt if not yet granted."""
    with suppress(Exception):
        from harness.computer import permissions
        permissions.request_screen_recording()


def _open_screen_recording_settings() -> None:
    with suppress(Exception):
        from harness.computer import permissions
        permissions.open_screen_recording_settings()


def _delete_project(project_id: str) -> bool:
    """Delete a project and everything under it: its locations, its sessions, and the
    per-(session, location) worktree records. (Remote worktree teardown over SSH is a
    follow-up — the DB rows go now.)"""
    assert state._session_factory is not None
    with sqlite_write_lock():
        database_session = state._session_factory()
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
    assert state._session_factory is not None
    with sqlite_write_lock():
        database_session = state._session_factory()
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
    assert state._session_factory is not None
    with sqlite_write_lock():
        database_session = state._session_factory()
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
    assert state._session_factory is not None
    with sqlite_write_lock():
        database_session = state._session_factory()
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


def _resolve_session_locations(context_id: str) -> list[dict[str, Any]] | None:
    """The runtime-shaped locations for a session's project: each entry carries the
    generated URI and the *effective* execution settings (own value, else project
    default). Returns ``None`` when the session has no project (so the runtime falls back
    to a single local location). Synchronous DB read — the executor calls it off-loop."""
    if state._session_factory is None:
        return None
    database_session = state._session_factory()
    try:
        session = database_session.get(SessionRecord, context_id)
        if session is None or not session.project_id:
            return None
        project = database_session.get(ProjectRecord, session.project_id)
        if project is None:
            return None
        locations = (
            database_session.query(LocationRecord)
            .filter(LocationRecord.project_id == project.id)
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
                "permission_mode": location.permission_mode or "default",
            })
        return resolved or None
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


def _reset_all_runtimes() -> None:
    """Drop cached agent runtimes so the next turn rebuilds its chat model. Used
    when the ChatGPT sign-in state changes (which lives in a token file, not the
    configuration, so the config watcher never fires for it)."""
    for executor in state._executors.values():
        executor.reset_runtimes()


def _validate_directory_payload(directory: str) -> dict[str, object]:
    if not directory:
        return {
            "valid": False,
            "exists": False,
            "is_directory": False,
            "is_absolute": False,
            "is_git_repository": False,
            "repository_root": "",
            "git_branch": "",
            "git_head": "",
            "git_short_head": "",
            "git_dirty": False,
            "git_detached": False,
            "git_label": "",
            "git_commit_subject": "",
            "git_commit_author": "",
            "git_commit_author_email": "",
            "git_commit_author_date": "",
            "git_upstream": "",
            "git_ahead": 0,
            "git_behind": 0,
            "git_staged_count": 0,
            "git_unstaged_count": 0,
            "git_untracked_count": 0,
            "git_conflicted_count": 0,
            "path": "",
        }
    path = Path(directory).expanduser()
    valid = path.is_absolute() and path.exists() and path.is_dir()
    is_git_repository = False
    repository_root = ""
    git_branch = ""
    git_head = ""
    git_short_head = ""
    git_dirty = False
    git_detached = False
    git_label = ""
    git_commit_subject = ""
    git_commit_author = ""
    git_commit_author_email = ""
    git_commit_author_date = ""
    git_upstream = ""
    git_ahead = 0
    git_behind = 0
    git_staged_count = 0
    git_unstaged_count = 0
    git_untracked_count = 0
    git_conflicted_count = 0
    if valid:
        try:
            inside = _run_git_probe(path, "rev-parse", "--is-inside-work-tree")
            is_git_repository = inside.returncode == 0 and inside.stdout.strip() == "true"
            if is_git_repository:
                root = _run_git_probe(path, "rev-parse", "--show-toplevel")
                if root.returncode == 0:
                    repository_root = root.stdout.strip()
                branch = _run_git_probe(path, "symbolic-ref", "--quiet", "--short", "HEAD")
                git_branch = branch.stdout.strip() if branch.returncode == 0 else ""
                head = _run_git_probe(path, "rev-parse", "HEAD")
                git_head = head.stdout.strip() if head.returncode == 0 else ""
                short_head = _run_git_probe(path, "rev-parse", "--short", "HEAD")
                git_short_head = short_head.stdout.strip() if short_head.returncode == 0 else ""
                commit = _run_git_probe(path, "cat-file", "-p", "HEAD")
                if commit.returncode == 0:
                    commit_metadata = _git_commit_metadata(commit.stdout)
                    git_commit_subject = commit_metadata["subject"]
                    git_commit_author = commit_metadata["author"]
                    git_commit_author_email = commit_metadata["author_email"]
                    git_commit_author_date = commit_metadata["author_date"]
                upstream = _run_git_probe(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
                git_upstream = upstream.stdout.strip() if upstream.returncode == 0 else ""
                if git_upstream:
                    ahead_behind = _run_git_probe(path, "rev-list", "--left-right", "--count", "HEAD...@{u}")
                    if ahead_behind.returncode == 0:
                        counts = ahead_behind.stdout.strip().split()
                        if len(counts) == 2:
                            git_ahead = int(counts[0])
                            git_behind = int(counts[1])
                staged = _run_git_probe(path, "diff", "--cached", "--name-only")
                git_staged_count = len(staged.stdout.splitlines()) if staged.returncode == 0 else 0
                unstaged = _run_git_probe(path, "diff", "--name-only")
                git_unstaged_count = len(unstaged.stdout.splitlines()) if unstaged.returncode == 0 else 0
                untracked = _run_git_probe(path, "ls-files", "--others", "--exclude-standard")
                git_untracked_count = len(untracked.stdout.splitlines()) if untracked.returncode == 0 else 0
                conflicted = _run_git_probe(path, "diff", "--name-only", "--diff-filter=U")
                git_conflicted_count = len(conflicted.stdout.splitlines()) if conflicted.returncode == 0 else 0
                git_dirty = any(
                    count > 0
                    for count in (
                        git_staged_count,
                        git_unstaged_count,
                        git_untracked_count,
                        git_conflicted_count,
                    )
                )
                git_detached = bool(git_head and not git_branch)
                git_label = git_branch or git_short_head
        except (FileNotFoundError, subprocess.SubprocessError, subprocess.TimeoutExpired):
            is_git_repository = False
    return {
        "valid": valid,
        "exists": path.exists(),
        "is_directory": path.is_dir(),
        "is_absolute": path.is_absolute(),
        "is_git_repository": is_git_repository,
        "repository_root": repository_root,
        "git_branch": git_branch,
        "git_head": git_head,
        "git_short_head": git_short_head,
        "git_dirty": git_dirty,
        "git_detached": git_detached,
        "git_label": git_label,
        "git_commit_subject": git_commit_subject,
        "git_commit_author": git_commit_author,
        "git_commit_author_email": git_commit_author_email,
        "git_commit_author_date": git_commit_author_date,
        "git_upstream": git_upstream,
        "git_ahead": git_ahead,
        "git_behind": git_behind,
        "git_staged_count": git_staged_count,
        "git_unstaged_count": git_unstaged_count,
        "git_untracked_count": git_untracked_count,
        "git_conflicted_count": git_conflicted_count,
        "path": str(path),
    }


def _git_commit_metadata(commit_text: str) -> dict[str, str]:
    headers, separator, message = commit_text.partition("\n\n")
    metadata = {
        "subject": "",
        "author": "",
        "author_email": "",
        "author_date": "",
    }
    for line in headers.splitlines():
        if not line.startswith("author "):
            continue
        match = re.match(r"author (.+) <([^>]+)> (\d+) ([+-]\d{4})$", line)
        if not match:
            continue
        metadata["author"] = match.group(1)
        metadata["author_email"] = match.group(2)
        timestamp = int(match.group(3))
        timezone_text = match.group(4)
        timezone_offset = timezone(
            timedelta(
                hours=int(timezone_text[1:3]),
                minutes=int(timezone_text[3:5]),
            ) * (1 if timezone_text[0] == "+" else -1)
        )
        metadata["author_date"] = datetime.fromtimestamp(timestamp, timezone_offset).isoformat()
        break
    if not separator:
        return metadata
    for line in message.splitlines():
        subject = line.strip()
        if subject:
            metadata["subject"] = subject
            return metadata
    return metadata


def _run_git_probe(directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        ["git", "-C", str(directory), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=5,
    )


def _git_status_key(payload: dict[str, object]) -> tuple[object, ...]:
    return (
        payload.get("valid"),
        payload.get("is_git_repository"),
        payload.get("repository_root"),
        payload.get("git_branch"),
        payload.get("git_head"),
        payload.get("git_short_head"),
        payload.get("git_dirty"),
        payload.get("git_detached"),
        payload.get("git_label"),
        payload.get("git_commit_subject"),
        payload.get("git_commit_author"),
        payload.get("git_commit_author_email"),
        payload.get("git_commit_author_date"),
        payload.get("git_upstream"),
        payload.get("git_ahead"),
        payload.get("git_behind"),
        payload.get("git_staged_count"),
        payload.get("git_unstaged_count"),
        payload.get("git_untracked_count"),
        payload.get("git_conflicted_count"),
    )


_GIT_STATUS_WATCH_FILTER = DefaultFilter(ignore_dirs=())


def _resolve_git_path(repository_root: Path, path_text: str) -> Path | None:
    if not path_text:
        return None
    path = Path(path_text)
    if not path.is_absolute():
        path = repository_root / path
    return path.resolve(strict=False)


def _git_status_watch_paths(directory: str, payload: dict[str, object]) -> list[str]:
    repository_root_text = str(payload.get("repository_root") or "")
    if not repository_root_text:
        return []
    repository_root = Path(repository_root_text).expanduser().resolve(strict=False)
    paths: list[Path] = [repository_root]
    for arguments in (("rev-parse", "--git-dir"), ("rev-parse", "--git-common-dir")):
        result = _run_git_probe(Path(directory), *arguments)
        if result.returncode != 0:
            continue
        path = _resolve_git_path(repository_root, result.stdout.strip())
        if path is not None:
            paths.append(path)

    seen: set[str] = set()
    existing_paths: list[str] = []
    for path in paths:
        path_text = str(path)
        if path_text in seen or not path.exists():
            continue
        seen.add(path_text)
        existing_paths.append(path_text)
    return existing_paths


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _git_status_changes_relevant(directory: str, payload: dict[str, object], changes: set[tuple[object, str]]) -> bool:
    repository_root_text = str(payload.get("repository_root") or "")
    if not repository_root_text:
        return True
    repository_root = Path(repository_root_text).expanduser().resolve(strict=False)
    git_paths = [
        path
        for path in (
            _resolve_git_path(repository_root, _run_git_probe(Path(directory), "rev-parse", "--git-dir").stdout.strip()),
            _resolve_git_path(repository_root, _run_git_probe(Path(directory), "rev-parse", "--git-common-dir").stdout.strip()),
        )
        if path is not None
    ]

    worktree_paths: list[str] = []
    for _change, path_text in changes:
        changed_path = Path(path_text).resolve(strict=False)
        if any(_is_relative_to(changed_path, git_path) for git_path in git_paths):
            return True
        if _is_relative_to(changed_path, repository_root):
            worktree_paths.append(str(changed_path.relative_to(repository_root)))

    if not worktree_paths:
        return False

    check_ignore = subprocess.run(
        ["git", "-C", str(Path(directory)), "check-ignore", "--stdin"],
        input="\n".join(worktree_paths),
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        timeout=5,
    )
    if check_ignore.returncode == 128:
        return True
    ignored = set(check_ignore.stdout.splitlines())
    return any(path not in ignored for path in worktree_paths)


def _open_folder_picker() -> dict[str, object]:
    system = platform.system()
    try:
        if system == "Darwin":
            result = subprocess.run(
                ["osascript", "-e", 'POSIX path of (choose folder with prompt "Choose a working directory")'],
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
            return _folder_picker_result(result)
        if system == "Windows":
            command = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
                "$dialog.Description = 'Choose a working directory'; "
                "$dialog.ShowNewFolderButton = $true; "
                "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
                "{ $dialog.SelectedPath }"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", command],
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
            return _folder_picker_result(result)
        result = _run_unix_folder_picker()
        if result is not None:
            return _folder_picker_result(result)
        return {
            "path": "",
            "cancelled": True,
            "error": "No supported graphical folder picker is available.",
        }
    except subprocess.TimeoutExpired:
        return {"path": "", "cancelled": True, "error": "Folder selection timed out."}
    except FileNotFoundError as exception:
        return {"path": "", "cancelled": True, "error": f"Folder picker is unavailable: {exception.filename}"}


def _folder_picker_result(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    if result.returncode != 0:
        return {"path": "", "cancelled": True, "error": result.stderr.strip()}
    selected_path = result.stdout.strip()
    if not selected_path:
        return {"path": "", "cancelled": True}
    return {"path": str(Path(selected_path).expanduser().resolve()), "cancelled": False}


def _run_unix_folder_picker() -> subprocess.CompletedProcess[str] | None:
    if shutil.which("zenity"):
        return subprocess.run(
            ["zenity", "--file-selection", "--directory", "--title=Choose a working directory"],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    if shutil.which("kdialog"):
        return subprocess.run(
            ["kdialog", "--getexistingdirectory", str(Path.home())],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    return _run_tk_folder_picker()


def _run_tk_folder_picker() -> subprocess.CompletedProcess[str] | None:
    script = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk()\n"
        "root.withdraw()\n"
        "path = filedialog.askdirectory(title='Choose a working directory')\n"
        "print(path or '')\n"
        "root.destroy()\n"
    )
    try:
        return subprocess.run(
            ["python3", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None


def _session_draft(context_id: str) -> str:
    assert state._session_factory is not None
    database_session = state._session_factory()
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
    assert state._session_factory is not None
    with sqlite_write_lock():
        database_session = state._session_factory()
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






# A real login session (a console getty, sshd, `login(1)`) never inherits a parent
# process's environment — it builds a fresh one from the OS user database and lets the
# login shell rebuild the rest from the system/user rc files. This server, by contrast,
# is frequently started from an already-mutated parent (a dev shell, a nix/direnv
# project, a virtualenv, `load_dotenv()` injecting secrets), so handing `pty.fork()` the
# server's own `os.environ` would leak that process state — stale DIRENV_* diffs,
# IN_NIX_SHELL, VIRTUAL_ENV, .env secrets — into every terminal, and it would never
# behave like a freshly spawned shell.
#
# We therefore replicate what `login` does instead of curating an allowlist (curating is
# both leaky — it forwards whatever pollution happens to sit in those names — and
# fragile across machines). We seed only the identity variables that the login layer,
# not the rc files, is responsible for, reading them from the passwd entry of the uid we
# are running as. This is portable by construction: on a Linux box, a remote host, or
# someone else's machine, `getpwuid` returns *that* system's HOME/SHELL, not a guess.






















# A file served for an ``open_artifact`` preview may live on a REMOTE location, so the
# session + location ride in a sentinel first path segment (``@ctx=<base64url json>``)
# rather than a query string: a page's relative sibling assets (``style.css``,
# ``app.js``) inherit the path prefix automatically but would drop a query string, so
# the query-param form would break multi-file remote pages. A local file carries no such
# prefix and is read straight off disk (the common, fast path).
_ARTIFACT_CONTEXT_PREFIX = "@ctx="


def _decode_artifact_context(segment: str) -> tuple[str, str]:
    """Decode a ``@ctx=`` path segment into ``(session, location_uri)``; ("","") if it
    is malformed (falls back to local-disk serving)."""
    try:
        raw = segment[len(_ARTIFACT_CONTEXT_PREFIX):]
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        data = json.loads(decoded)
        return str(data.get("s", "")), str(data.get("l", ""))
    except (ValueError, TypeError):
        return "", ""




# A rewriting pass-through proxy for `open_artifact` of external URLs. It serves
# the page — and *every* asset and request it makes — back through this one route,
# so to the framed page everything looks same-origin (our localhost). That is what
# lets sites that refuse direct framing (`X-Frame-Options`/`frame-ancestors`) render,
# and avoids the cross-origin CORS/history errors a naive `<base>` proxy hits.

_PROXY_PATH = "/artifact-proxy"
_PROXY_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
# URL schemes that must never be rewritten through the proxy.
_PROXY_SKIP_SCHEMES = ("data:", "blob:", "javascript:", "mailto:", "tel:", "about:", "#", "vbscript:")
# Response headers dropped when re-serving (framing blockers + hop-by-hop/encoding
# headers that no longer match the rewritten body). set-cookie is dropped from the
# BROWSER response (its cookies would be scoped to our localhost origin, useless)
# but the cookies are still stored server-side by the shared cookie-jar client and
# replayed upstream, so login/consent/session flows survive across proxied requests.
_PROXY_DROP_HEADERS = {
    "x-frame-options",
    "content-security-policy",
    "content-security-policy-report-only",
    "cross-origin-opener-policy",
    "cross-origin-embedder-policy",
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "set-cookie",
    "strict-transport-security",
    "report-to",
    "reporting-endpoints",
}
# Request headers never forwarded upstream — hop-by-hop, or ones httpx/the target
# must recompute for the real origin rather than inherit from our localhost frame.
_PROXY_DROP_REQUEST_HEADERS = {
    "host",
    "connection",
    "keep-alive",
    "content-length",
    "accept-encoding",
    "origin",
    "referer",
    "cookie",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-dest",
    "sec-fetch-user",
}

# One long-lived client so the upstream cookie jar (session, consent, CSRF cookies)
# persists across every proxied request. Cookies are domain-scoped by httpx, so
# different opened sites never share them. Created lazily on the running loop.


def _get_proxy_client() -> httpx.AsyncClient:
    if state._proxy_client is None:
        state._proxy_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            headers=_PROXY_BROWSER_HEADERS,
        )
    return state._proxy_client

_PROXY_HTML_ATTR_RE = re.compile(
    r'(?P<pre>\b(?:src|href|action|formaction|poster|data-src|data-href|data-url)\s*=\s*)'
    r'(?P<q>["\'])(?P<url>[^"\']*)(?P=q)',
    re.IGNORECASE,
)
_PROXY_HTML_SRCSET_RE = re.compile(r'(?P<pre>\bsrcset\s*=\s*)(?P<q>["\'])(?P<val>[^"\']*)(?P=q)', re.IGNORECASE)
_PROXY_STYLE_BLOCK_RE = re.compile(r'(<style[^>]*>)(?P<body>.*?)(</style>)', re.IGNORECASE | re.DOTALL)
_PROXY_CSS_URL_RE = re.compile(r'url\(\s*(?P<q>["\']?)(?P<url>[^)"\']+)(?P=q)\s*\)', re.IGNORECASE)
_PROXY_CSS_IMPORT_RE = re.compile(r'(?P<pre>@import\s+)(?P<q>["\'])(?P<url>[^"\']+)(?P=q)', re.IGNORECASE)
# Tags/attributes that would fight the proxy: an inline CSP, a <base> that would
# re-point relative URLs, and SRI/crossorigin hints that fail once same-origin.
_PROXY_CSP_META_RE = re.compile(r'<meta[^>]+http-equiv\s*=\s*["\']?content-security-policy[^>]*>', re.IGNORECASE)
_PROXY_BASE_TAG_RE = re.compile(r'<base[^>]*>', re.IGNORECASE)
_PROXY_STRIP_ATTR_RE = re.compile(
    r'\s+(?:integrity|crossorigin|nonce)(?:\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+))?',
    re.IGNORECASE,
)


def _proxy_ref(raw: str, base: str) -> str:
    """Resolve ``raw`` (possibly relative) against ``base`` and route it back through
    this proxy. Schemes that are not real fetches (data:, javascript:, #…) pass through."""
    target = raw.strip()
    if not target or target.lower().startswith(_PROXY_SKIP_SCHEMES):
        return raw
    absolute = urljoin(base, target)
    if not absolute.lower().startswith(("http://", "https://")):
        return raw
    return f"{_PROXY_PATH}?url={quote(absolute, safe='')}"


def _rewrite_proxy_css(text: str, base: str) -> str:
    text = _PROXY_CSS_URL_RE.sub(
        lambda match: f'url({match.group("q")}{_proxy_ref(match.group("url"), base)}{match.group("q")})', text
    )
    text = _PROXY_CSS_IMPORT_RE.sub(
        lambda match: f'{match.group("pre")}{match.group("q")}{_proxy_ref(match.group("url"), base)}{match.group("q")}', text
    )
    return text


# ES-module specifiers the browser resolves itself (static import/export-from and
# string-literal dynamic import). Served from our /artifact-proxy path, relative
# specifiers would otherwise resolve against localhost and 404 — so every literal
# specifier is rewritten to an absolute, proxied URL. Computed specifiers in a
# dynamic import() cannot be rewritten statically (the runtime shim cannot patch the
# import operator either); those remain a known gap.
_PROXY_JS_STATIC_IMPORT_RE = re.compile(
    r'(?P<pre>\b(?:import|export)\b[^;\n]*?\bfrom\s*)(?P<q>["\'])(?P<url>[^"\']+)(?P=q)',
    re.IGNORECASE,
)
_PROXY_JS_BARE_IMPORT_RE = re.compile(
    r'(?P<pre>\bimport\s*)(?P<q>["\'])(?P<url>[^"\']+)(?P=q)',
    re.IGNORECASE,
)
_PROXY_JS_DYNAMIC_IMPORT_RE = re.compile(
    r'(?P<pre>\bimport\s*\(\s*)(?P<q>["\'])(?P<url>[^"\']+)(?P=q)(?P<post>\s*\))',
    re.IGNORECASE,
)


def _rewrite_proxy_js(text: str, base: str) -> str:
    """Rewrite ES-module import/export specifiers in a served script to proxied,
    absolute URLs. Applied to any response whose content type is JavaScript; the
    patterns only touch module syntax, which a classic script would not contain."""
    text = _PROXY_JS_STATIC_IMPORT_RE.sub(
        lambda match: f'{match.group("pre")}{match.group("q")}{_proxy_ref(match.group("url"), base)}{match.group("q")}', text
    )
    text = _PROXY_JS_DYNAMIC_IMPORT_RE.sub(
        lambda match: f'{match.group("pre")}{match.group("q")}{_proxy_ref(match.group("url"), base)}{match.group("q")}{match.group("post")}', text
    )
    text = _PROXY_JS_BARE_IMPORT_RE.sub(
        lambda match: f'{match.group("pre")}{match.group("q")}{_proxy_ref(match.group("url"), base)}{match.group("q")}', text
    )
    return text


_PROXY_IMPORTMAP_RE = re.compile(
    r'(<script[^>]*\btype\s*=\s*["\']importmap["\'][^>]*>)(?P<body>.*?)(</script>)',
    re.IGNORECASE | re.DOTALL,
)


def _rewrite_importmap_urls(node: Any, base: str) -> Any:
    """Route every URL value in an import map (imports + scopes) through the proxy so
    bare specifiers the map resolves still load from the real origin."""
    if isinstance(node, dict):
        return {key: _rewrite_importmap_urls(value, base) for key, value in node.items()}
    if isinstance(node, str):
        return _proxy_ref(node, base)
    return node


def _rewrite_proxy_importmap(markup: str, base: str) -> str:
    def _replace(match: re.Match) -> str:
        try:
            parsed = json.loads(match.group("body"))
        except (ValueError, TypeError):
            return match.group(0)
        rewritten = json.dumps(_rewrite_importmap_urls(parsed, base))
        return f"{match.group(1)}{rewritten}{match.group(3)}"

    return _PROXY_IMPORTMAP_RE.sub(_replace, markup)


def _rewrite_proxy_srcset(value: str, base: str) -> str:
    rewritten = []
    for candidate in value.split(","):
        chunk = candidate.strip()
        if not chunk:
            continue
        bits = chunk.split(None, 1)
        descriptor = f" {bits[1]}" if len(bits) > 1 else ""
        rewritten.append(f"{_proxy_ref(bits[0], base)}{descriptor}")
    return ", ".join(rewritten)


_PROXY_RUNTIME_TEMPLATE = (ASSETS_DIRECTORY / "proxy_runtime.js").read_text(encoding="utf-8")


def _proxy_runtime(base: str) -> str:
    """A small shim injected into every proxied page so URLs built *by scripts*
    (fetch/XHR, history navigations, dynamically created elements) also go through
    the proxy and resolve against the real origin — not our localhost — and so a
    cross-origin ``history.replaceState`` no longer throws. The script itself lives
    in ``assets/proxy_runtime.js``; the per-page origin/prefix are substituted in."""
    source = (
        _PROXY_RUNTIME_TEMPLATE
        .replace("__DAISY_PROXY_BASE__", json.dumps(base))
        .replace("__DAISY_PROXY_URL__", json.dumps(f"{_PROXY_PATH}?url="))
        .replace("__DAISY_WS_PROXY_URL__", json.dumps("/artifact-proxy-ws?url="))
    )
    return f"""<script>
{source}
</script>"""


def _rewrite_proxy_html(markup: str, base: str) -> str:
    markup = _PROXY_CSP_META_RE.sub("", markup)
    markup = _PROXY_BASE_TAG_RE.sub("", markup)
    markup = _PROXY_STRIP_ATTR_RE.sub("", markup)
    # Import maps first — their JSON must be rewritten before the generic attribute
    # pass could disturb the <script> body.
    markup = _rewrite_proxy_importmap(markup, base)
    markup = _PROXY_HTML_ATTR_RE.sub(
        lambda match: f'{match.group("pre")}{match.group("q")}{_proxy_ref(match.group("url"), base)}{match.group("q")}', markup
    )
    markup = _PROXY_HTML_SRCSET_RE.sub(
        lambda match: f'{match.group("pre")}{match.group("q")}{_rewrite_proxy_srcset(match.group("val"), base)}{match.group("q")}', markup
    )
    markup = _PROXY_STYLE_BLOCK_RE.sub(
        lambda match: f'{match.group(1)}{_rewrite_proxy_css(match.group("body"), base)}{match.group(3)}', markup
    )
    runtime = _proxy_runtime(base)
    head_match = re.search(r"<head[^>]*>", markup, re.IGNORECASE)
    if head_match:
        return markup[: head_match.end()] + runtime + markup[head_match.end() :]
    return runtime + markup


def _proxy_forward_headers(request: Request, target_url: str) -> dict[str, str]:
    """The request headers to forward upstream: the browser's own headers (so the
    site sees a real browser) minus hop-by-hop/localhost-specific ones, with Origin
    and Referer rewritten to the target's own origin rather than our localhost frame
    (many APIs reject a mismatched Origin, or vary their response by Referer)."""
    forwarded = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _PROXY_DROP_REQUEST_HEADERS
    }
    parsed = urlparse(target_url)
    if parsed.scheme and parsed.netloc:
        forwarded["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
        forwarded["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    return forwarded


def _remove_upload_file(path_string: str, uploads_root: str) -> None:
    """Delete an orphaned content-addressed upload from Daisy's uploads directory."""
    path = Path(path_string)
    root = Path(uploads_root)
    if path.parent != root:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _prune_session_artifacts(context_id: str) -> None:
    """Retention on session delete: drop this session's branch in every shadow repo it
    touched, then delete its artifact index rows (versions/files/surfaces/annotations) and
    its persisted conversation and lifecycle facts. Runs off the event loop. Best-effort
    per repo — a missing/unreachable location's branch is left, but its DB rows are still
    cleared."""
    if state._session_factory is None:
        return
    database_session = state._session_factory()
    try:
        repositories = {
            (cast(str, location_uri), cast(str, git_directory))
            for location_uri, git_directory in database_session.query(
                ArtifactVersionRecord.location_uri, ArtifactVersionRecord.git_directory
            ).filter(ArtifactVersionRecord.context_id == context_id).distinct()
        }
    finally:
        database_session.close()
    for location_uri, git_directory in repositories:
        executor = _executor_for_location_uri(context_id, location_uri)
        if executor is None:
            continue
        try:
            artifacts.prune_session(executor, git_directory, context_id)
        except Exception:
            _artifact_logger.exception("failed to prune session branch in %s", git_directory)
    with sqlite_write_lock():
        database_session = state._session_factory()
        try:
            for model in (
                ArtifactVersionRecord,
                ArtifactFileRecord,
                ArtifactSurfaceRecord,
                ArtifactAnnotationRecord,
                SessionLifecycleRecord,
            ):
                database_session.query(model).filter(model.context_id == context_id).delete(synchronize_session=False)
            database_session.commit()
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


def _is_loopback_bind(host: str) -> bool:
    """Whether a bind host keeps the server off the network (loopback only). ``0.0.0.0`` /
    ``::`` (all interfaces) and any concrete LAN address are *not* loopback — they expose it."""
    normalized = (host or "").strip().lower()
    if normalized in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _assert_exposure_authenticated(configuration: "GlobalConfiguration") -> None:
    """Fail closed on exposure: the only inbound auth the server has is the A2A config, and
    the REST + A2A surfaces execute tools. Binding to anything but loopback without that auth
    configured would serve tool execution to an unauthenticated network by omission, so refuse
    to start rather than silently exposing it. Loopback (the desktop app's default) is
    unaffected."""
    if _is_loopback_bind(state._BIND_HOST):
        return
    a2a = getattr(configuration, "a2a", None)
    if a2a is None or not a2a.enabled():
        raise RuntimeError(
            f"Refusing to start: bound to non-loopback host {state._BIND_HOST!r} without inbound auth. "
            "Configure the [a2a] api_key or oauth2_jwks_url before exposing the server, or bind to "
            "127.0.0.1."
        )
