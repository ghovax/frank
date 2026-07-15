import asyncio
import base64
import fcntl
import hashlib
import uuid
import json
import logging
import mimetypes
import os
import posixpath
import platform
import pty
import pwd
import re
import shutil
import signal
import struct
from itertools import combinations
import subprocess
import termios

import httpx
from collections import deque
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional, cast
from urllib.parse import quote, urljoin, urlparse

import websockets
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from sqlalchemy import Boolean, Column, Index, Integer, String, Text, create_engine, event, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sse_starlette.sse import EventSourceResponse
from watchfiles import DefaultFilter, awatch
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, messages_from_dict, messages_to_dict
from pydantic import BaseModel, Field

from a2a.server.apps.jsonrpc import A2AFastAPIApplication
from a2a.server.request_handlers import DefaultRequestHandler

from harness.core.a2a_executor import (
    AgentRegistry,
    HarnessAgentExecutor,
    agent_rpc_path,
    build_agent_card,
)
from harness.core.agent import AgentRuntime, build_chat_model, model_is_authorized
from harness.core.task_store import AppendOnlyTaskStore
import harness.core.configuration as _configuration
from harness.core.configuration import (
    GlobalConfiguration,
    PromptLoader,
    agent_configuration_path,
    configuration_file_path,
    database_file_path,
    harness_home_directory,
    list_agent_route_names,
    list_agents,
    load_agent_configuration,
    save_api_keys,
    seed_home_agents,
)
from harness.core.chatgpt_oauth import (
    ChatGPTLoginFlow,
    clear_tokens,
    load_tokens,
)
from harness.core.codex_model import (
    clear_subscription_models_cache,
    clear_usage_snapshot,
    fetch_subscription_models,
    get_usage_snapshot,
)
from harness.core.composio_router import composio_mcp_servers
from harness.core.mcp_client import MCPClientManager
from harness.core.models import MODELS, ModelDefinition, available_models, find_model, provider_and_suffix
from harness.core.providers import PROVIDERS
from harness.core.background import reap_orphaned_processes
from harness.core.file_leases import FileLeaseManager
from harness.core.session_workspaces import SessionWorkspace, SessionWorkspaceManager, WorkspaceStrategy
from harness.core.sqlite_lock import configure_sqlite_lock, sqlite_write_lock
from harness.core.skills import load_skills, skills_for_agent
from harness.locations import ssh_hosts as _ssh_hosts
from harness.locations.executor import LocationExecutor, SshExecutor
from harness.locations.resolver import LocationAddress, executor_for, host_is_defined, location_uri_for
from harness.core import artifact_versioning as artifacts
from harness.tools.tools import (
    ASSETS_DIRECTORY,
    cancel_all_background_tasks,
    set_exa_client,
    set_mcp_client_manager,
    _inject_artifact_runtime,
)
from harness.tools.file_tools import set_firecrawl_client, set_jina_api_key, set_proxy_url
from harness.core.tuning import set_tuning, tuning_from_policy

# Load .env (gitignored) so API keys are available via the environment without
# being stored in the tracked configuration.yaml. Existing env vars win, so a
# direnv-provided environment is not overridden.
load_dotenv()

PUBLIC_BASE_URL = "http://localhost:8822"
AGENT_CARD_PATH = "/.well-known/agent-card.json"

Base = declarative_base()


class SessionRecord(Base):
    """A chat session — one A2A context. Tasks live in the A2A task store; this
    table only indexes sessions for the sidebar."""

    __tablename__ = "sessions"

    id = Column(String, primary_key=True)  # == A2A contextId
    agent = Column(String, nullable=False)
    # The project this session belongs to; the agent may address any of the project's
    # locations per tool call.
    project_id = Column(String, default="")
    # Source path selected in the UI. Project-local agents/skills/instructions
    # are resolved from here.
    working_directory = Column(Text, default="")
    # Actual path where shell and file tools run. For Git projects this is a
    # per-session worktree; for non-Git directories it falls back to the source.
    runtime_working_directory = Column(Text, default="")
    workspace_strategy = Column(Text, default="none")
    workspace_path = Column(Text, default="")
    workspace_branch = Column(Text, default="")
    source_repository_root = Column(Text, default="")
    runtime_repository_root = Column(Text, default="")
    workspace_head = Column(Text, default="")
    workspace_error = Column(Text, default="")
    title = Column(Text, default="")
    # Per-session permission mode for future turns and frontend hydration.
    permission_mode = Column(Text, default="default")
    input_draft = Column(Text, default="")
    created_at = Column(String, nullable=False)

    __table_args__ = (
        Index("idx_sessions_created_at", "created_at"),
        Index("idx_sessions_project", "project_id"),
    )


class ProjectRecord(Base):
    """A project — the top-level unit of work. A named, described container that owns a
    set of locations (local + SSH remotes) and the sessions run against them. Server-owned
    domain data: the server reads it and is the source of truth (unlike ~/.ssh/config and
    configuration.yaml, which are OS/global files)."""

    __tablename__ = "projects"

    id = Column(String, primary_key=True)  # generated uuid
    name = Column(Text, nullable=False)
    description = Column(Text, default="")
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)


class LocationRecord(Base):
    """A named place a project runs tools in: the home server's own filesystem
    (``kind="local"``) or a remote reached over SSH (``kind="remote"``, referencing a
    ``~/.ssh/config`` host alias). ``permission_mode`` is the one execution policy a
    location carries (``read_only`` etc. is enforced per tool call); ``name`` is derived
    from the connection (host alias / folder), not user-entered. The model-facing location
    URI is generated from the resolved connection, not stored (so it can't go stale)."""

    __tablename__ = "locations"

    id = Column(String, primary_key=True)  # generated uuid
    project_id = Column(String, nullable=False)
    name = Column(Text, nullable=False)  # derived, project-scoped, e.g. "local", "prod"
    kind = Column(Text, nullable=False)  # "local" | "remote"
    host_alias = Column(Text, default="")  # ~/.ssh/config alias, remote only
    base_directory = Column(Text, nullable=False)
    permission_mode = Column(Text, default="default")  # default | auto | read_only | bypass
    created_at = Column(String, nullable=False)

    __table_args__ = (Index("idx_locations_project", "project_id"),)


class ModelHistoryRecord(Base):
    """Recently selected models (provider/model id + label), mirroring the project
    history so a user can quickly switch back to a model they used before."""

    __tablename__ = "model_history"

    model_id = Column(Text, primary_key=True)
    name = Column(Text, default="")
    provider = Column(Text, default="")
    selected_at = Column(String, nullable=False)

    __table_args__ = (Index("idx_model_history_selected_at", "selected_at"),)


class ConversationRecord(Base):
    """The agent's dialogue history (LangChain messages) per A2A context, persisted
    so a session keeps its context across a server restart. The A2A task store holds
    the transcript the UI replays; this holds the model-facing message list the agent
    actually resumes from."""

    __tablename__ = "conversations"

    context_id = Column(String, primary_key=True)  # == A2A contextId
    messages = Column(Text, default="")  # JSON: langchain messages_to_dict
    updated_at = Column(String, nullable=False)


class ArtifactVersionRecord(Base):
    """One captured version — a commit on a session branch in a shadow git repo. The
    shadow repo lives under the location's ``~/.daisy/versions`` and is driven with an
    explicit ``--git-dir``/``--work-tree`` so it never touches the user's own ``.git``
    (see ``core/artifact_versioning.py``). This row is the DB index into that git
    history: it lets the timeline list versions across every location without querying a
    (possibly remote) git repo. ``sequence`` is the version's 1-based position on the branch."""

    __tablename__ = "artifact_versions"

    id = Column(String, primary_key=True)
    context_id = Column(String, nullable=False)  # A2A contextId (the session)
    project_id = Column(String, default="")
    location_uri = Column(String, default="")
    git_directory = Column(String, nullable=False)
    work_tree = Column(String, nullable=False)
    branch = Column(String, nullable=False)
    commit_sha = Column(String, nullable=False)
    sequence = Column(Integer, nullable=False)
    message = Column(Text, default="")
    tool_call_id = Column(String, default="")
    created_at = Column(String, nullable=False)

    __table_args__ = (Index("idx_artifact_versions_context", "context_id", "created_at"),)


class ArtifactFileRecord(Base):
    """One file changed in a captured version. This is what powers the file-history view
    (``git log`` reconstructed from the DB): each row ties a ``(context, relative_path)`` to
    the commit that changed it and the blob sha of the new content, so a version's bytes
    can be streamed with ``git cat-file`` from whichever location owns the shadow repo.
    Over-cap files are recorded as placeholders (no ``blob_sha``)."""

    __tablename__ = "artifact_files"

    id = Column(String, primary_key=True)
    version_id = Column(String, nullable=False)  # -> ArtifactVersionRecord.id
    context_id = Column(String, nullable=False)  # denormalized for fast per-session listing
    location_uri = Column(String, default="")
    git_directory = Column(String, nullable=False)  # denormalized so serving needs no join
    work_tree = Column(String, nullable=False)
    commit_sha = Column(String, nullable=False)
    relative_path = Column(String, nullable=False)
    absolute_path = Column(Text, default="")
    blob_sha = Column(String, default="")  # "" for deletions and placeholders
    change_type = Column(String, default="M")  # "A" | "M" | "D"
    size = Column(Integer, default=0)
    is_placeholder = Column(Boolean, default=False)
    created_at = Column(String, nullable=False)

    __table_args__ = (Index("idx_artifact_files_context_path", "context_id", "relative_path"),)


class ArtifactSurfaceRecord(Base):
    """An artifact the agent explicitly surfaced with ``open_artifact`` — i.e. one that
    earns a tab in the artifacts panel. Capture is silent for *everything* the agent
    writes; surfacing is the curated subset. ``id`` is the stable surface id so
    re-opening the same file updates one tab. For a live external URL (an ``iframe``
    with no local file) there is no version history — ``git_directory``/``relative_path`` are empty
    and ``source`` holds the URL."""

    __tablename__ = "artifact_surfaces"

    id = Column(String, primary_key=True)  # surface id (the artifact_id)
    context_id = Column(String, nullable=False)
    location_uri = Column(String, default="")
    git_directory = Column(String, default="")
    work_tree = Column(String, default="")
    relative_path = Column(String, default="")
    absolute_path = Column(Text, default="")
    kind = Column(String, default="image")  # "image" | "iframe" | "html"
    title = Column(Text, default="")
    source = Column(Text, default="")  # the original path/URL opened
    tool_call_id = Column(String, default="")
    latest_commit_sha = Column(String, default="")
    latest_blob_sha = Column(String, default="")
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)

    __table_args__ = (Index("idx_artifact_surfaces_context", "context_id", "created_at"),)


class ArtifactAnnotationRecord(Base):
    """Image annotations bound to one specific captured version (git commit sha). Keyed on
    ``(surface, version)`` so a regenerated image (a new commit) never inherits the
    previous version's pins."""

    __tablename__ = "artifact_annotations"

    context_id = Column(String, primary_key=True)
    surface_id = Column(String, primary_key=True)
    version_id = Column(String, primary_key=True)  # the commit sha (opaque string)
    annotations = Column(Text, default="")
    updated_at = Column(String, nullable=False)

    __table_args__ = (Index("idx_artifact_annotations_context_updated", "context_id", "updated_at"),)


class TerminalStateRecord(Base):
    """Persisted scrollback for a server-owned terminal session."""

    __tablename__ = "terminal_states"

    context_id = Column(String, primary_key=True)
    terminal_key = Column(String, primary_key=True)
    working_directory = Column(Text, default="")
    scrollback = Column(Text, default="")
    # Creation time, used to order a context's terminals into stable tabs; set once on
    # insert and never touched again (unlike updated_at, which moves on every write).
    created_at = Column(String, default="")
    updated_at = Column(String, nullable=False)

    __table_args__ = (Index("idx_terminal_states_updated", "updated_at"),)


class Broadcaster:
    """A tiny in-process pub/sub. Subscribers receive every broadcast event,
    which is how live changes (e.g. edited agents) reach connected clients."""

    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait(event)


_global_configuration: Optional[GlobalConfiguration] = None
_session_factory: Optional[sessionmaker] = None
_async_engine = None
_task_store: Optional[AppendOnlyTaskStore] = None
_registry: Optional[AgentRegistry] = None
_mcp_manager: Optional[MCPClientManager] = None
_main_loop: asyncio.AbstractEventLoop | None = None
_file_lease_manager: FileLeaseManager | None = None
_workspace_manager: SessionWorkspaceManager | None = None
_terminal_manager: "TerminalSessionManager | None" = None
# Composio Tool Router server(s), provisioned once at startup. Kept separate from
# the mcp.json-derived servers so the file watcher's live reload re-merges them
# instead of dropping Composio whenever mcp.json changes.
_composio_servers: dict[str, _configuration.MCPServerConfiguration] = {}
_executors: dict[str, HarnessAgentExecutor] = {}
_mounted_agents: set[str] = set()
_pending_permissions: dict[str, asyncio.Future] = {}
# Pending ask_user answers, keyed by request id (prefixed "q-<context_id>-").
# Mirrors _pending_permissions: the runtime awaits a future the UI resolves via
# the /chat/{context_id}/question endpoint.
_pending_questions: dict[str, asyncio.Future] = {}
# Dialogue history per A2A context, shared across every agent executor so that
# switching the active agent continues the same conversation (the persona is
# applied per-turn on top of this shared history).
_conversations: dict[str, list] = {}
# How many top-level turns are running per context. Drives the sidebar's
# "running" spinner; a count rather than a flag so overlapping turns are handled.
_running_contexts: dict[str, int] = {}


def _notify_filesystem_lease_state() -> None:
    _publish_broadcast({"type": "sessions_changed"})
    _publish_broadcast({"type": "filesystem_leases_changed"})


class _ContextEventBus:
    """Per-context fan-out of the structured A2A parts a turn emits.

    A non-driving viewer (e.g. the sidebar re-opened on a running session) follows
    the turn by subscribing here instead of polling the task store and re-replaying
    the whole transcript every second. ``publish`` is called from the executor
    *after* each part is persisted, and ``complete`` when the turn ends.

    Delivery is snapshot-then-tail and gap-/duplicate-free without a cursor: a
    subscriber takes a baseline ``high_seq``, reads a compacted snapshot of the
    persisted transcript (which covers every event with seq <= baseline, because
    publish runs after persist), then drains its queue for events with seq >
    baseline and keeps reading live.
    """

    # Sentinel placed on a subscriber's queue when the context's turn completes,
    # so the SSE generator can emit a terminal frame and close cleanly.
    _DONE = object()

    def __init__(self) -> None:
        self._seq: dict[str, int] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    def publish(self, context_id: str, part: dict) -> int:
        seq = self._seq.get(context_id, 0) + 1
        self._seq[context_id] = seq
        for queue in self._subscribers.get(context_id, ()):
            queue.put_nowait((seq, part))
        return seq

    def complete(self, context_id: str) -> None:
        for queue in self._subscribers.get(context_id, ()):
            queue.put_nowait(self._DONE)

    def high_seq(self, context_id: str) -> int:
        return self._seq.get(context_id, 0)

    def subscribe(self, context_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(context_id, []).append(queue)
        return queue

    def unsubscribe(self, context_id: str, queue: asyncio.Queue) -> None:
        subscribers = self._subscribers.get(context_id)
        if subscribers and queue in subscribers:
            subscribers.remove(queue)
            if not subscribers:
                self._subscribers.pop(context_id, None)


_event_bus = _ContextEventBus()


def _publish_stream_event(context_id: str, part) -> None:
    """Executor hook: serialize one structured part and fan it out to live viewers."""
    _event_bus.publish(context_id, part.model_dump(by_alias=True, exclude_none=True, mode="json"))


def _set_turn_state(context_id: str, running: bool) -> None:
    """Track active turns per context and broadcast on the empty/active edge so the
    sidebar reflects which conversations are currently running."""
    previous = _running_contexts.get(context_id, 0)
    updated = previous + 1 if running else max(0, previous - 1)
    if updated:
        _running_contexts[context_id] = updated
    else:
        _running_contexts.pop(context_id, None)
    if (previous == 0) != (updated == 0):
        _broadcaster.publish({"type": "sessions_changed"})
    # When the last turn for a context finishes, tell live viewers to do a final
    # refresh and close — the structured-part fan-out is only meaningful mid-turn.
    if not running and updated == 0:
        _event_bus.complete(context_id)


def _notify_permission_state(context_id: str) -> None:
    """A turn raised (or settled) a permission request — refresh the sidebar so it
    can swap the spinner for an attention marker on the waiting session."""
    _broadcaster.publish({"type": "sessions_changed"})

_broadcaster = Broadcaster()
# Keeps references to in-flight session-title generation tasks so they are not
# garbage-collected before completing.
_title_tasks: set[Any] = set()


def _publish_broadcast(event: dict) -> None:
    """Publish from either the event-loop thread or a worker thread."""
    if _main_loop is not None and _main_loop.is_running():
        _main_loop.call_soon_threadsafe(_broadcaster.publish, event)
    else:
        _broadcaster.publish(event)


def _schedule_session_title(context_id: str, first_message: str) -> None:
    if _main_loop is not None and _main_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(_finalize_session_title(context_id, first_message), _main_loop)
        _title_tasks.add(future)
        future.add_done_callback(_title_tasks.discard)
        return
    try:
        task = asyncio.create_task(_finalize_session_title(context_id, first_message))
        _title_tasks.add(task)
        task.add_done_callback(_title_tasks.discard)
    except RuntimeError:
        # No running event loop (e.g. called outside a request) — keep the provisional title.
        pass


def _load_conversation(context_id: str) -> list:
    """Restore a context's persisted dialogue history (LangChain messages). Returns
    an empty list when there is nothing stored or the stored form can't be decoded —
    a resumed session then simply starts fresh rather than erroring."""
    if _session_factory is None:
        return []
    database_session = _session_factory()
    try:
        record = database_session.get(ConversationRecord, context_id)
        if record is None or not record.messages:
            return []
        return messages_from_dict(json.loads(record.messages))
    except Exception:
        return []
    finally:
        database_session.close()


def _save_conversation(context_id: str, messages: list) -> None:
    """Persist a context's dialogue history after a turn, so it survives a restart."""
    if _session_factory is None or not context_id:
        return
    with sqlite_write_lock():
        database_session = _session_factory()
        try:
            serialized = json.dumps(messages_to_dict(messages))
            record = database_session.get(ConversationRecord, context_id)
            now = datetime.now(timezone.utc).isoformat()
            if record is None:
                database_session.add(ConversationRecord(context_id=context_id, messages=serialized, updated_at=now))
            else:
                record.messages = serialized
                record.updated_at = now
            database_session.commit()
        except Exception:
            database_session.rollback()
        finally:
            database_session.close()


def _session_agent_for(context_id: str) -> str:
    """Read a session's owning agent from its record (``""`` when unknown). Lets an
    on-demand action reach the right executor even before that agent has a live
    runtime this process (e.g. a session reopened after a restart)."""
    if _session_factory is None or not context_id:
        return ""
    database_session = _session_factory()
    try:
        record = database_session.get(SessionRecord, context_id)
        return (record.agent or "") if record is not None else ""
    except Exception:
        return ""
    finally:
        database_session.close()


def _session_working_directory_for(context_id: str) -> str:
    """Read a session's source working directory from its record."""
    if _session_factory is None or not context_id:
        return ""
    database_session = _session_factory()
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
    return _executors.get(agent_name) if agent_name else None


def _normalize_permission_mode(mode: str) -> str:
    return mode if mode in {"default", "auto", "read_only", "bypass"} else "default"


def _session_permission_mode_for(context_id: str) -> str:
    """Read a context's persisted permission mode for frontend hydration and
    runtime rebuilds. Missing/invalid values fall back to the agent default."""
    if _session_factory is None or not context_id:
        return "default"
    database_session = _session_factory()
    try:
        record = database_session.get(SessionRecord, context_id)
        return _normalize_permission_mode(record.permission_mode or "default") if record is not None else "default"
    except Exception:
        return "default"
    finally:
        database_session.close()


def _set_session_permission_mode(context_id: str, mode: str) -> bool:
    """Persist a session permission mode. Returns whether the session exists."""
    if _session_factory is None or not context_id:
        return False
    normalized = _normalize_permission_mode(mode)
    with sqlite_write_lock():
        database_session = _session_factory()
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
    assert _session_factory is not None
    source_directory = working_directory or str(Path.home())

    database_session = _session_factory()
    try:
        record = database_session.get(SessionRecord, context_id)
        if record is not None:
            workspace = _session_workspace_from_record(record)
            if workspace.runtime_working_directory:
                return workspace
    finally:
        database_session.close()

    requested_strategy = workspace_strategy if workspace_strategy in {"none", "branch", "worktree"} else ""
    strategy = cast(WorkspaceStrategy, requested_strategy or (_global_configuration.workspace.strategy if _global_configuration is not None else "none"))
    if _workspace_manager is not None:
        workspace = _workspace_manager.prepare_sync(context_id, source_directory, strategy)
    else:
        resolved = str(Path(source_directory).expanduser().resolve(strict=False))
        workspace = SessionWorkspace(
            source_working_directory=resolved,
            runtime_working_directory=resolved,
            strategy="none",
            error="Session workspace manager is not initialized.",
        )

    with sqlite_write_lock():
        database_session = _session_factory()
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
    project-history list. Catalog models use their curated label; typed model ids
    derive a readable label from the provider/model value."""
    if not model_identifier or _session_factory is None:
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
        database_session = _session_factory()
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
    if _session_factory is None:
        return []
    database_session = _session_factory()
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


_capture_queue: "asyncio.Queue[_CaptureRequest] | None" = None


def _artifact_maximum_bytes() -> int:
    """The per-file byte cap above which a write is recorded as a placeholder version."""
    workspace = getattr(_global_configuration, "workspace", None) if _global_configuration else None
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
    if _capture_queue is None:
        return
    request = _CaptureRequest(
        context_id=context_id, location_uri=location_uri, executor=executor,
        base_directory=base_directory, changed_absolute_paths=changed_absolute_paths,
        mode=mode, original_contents=original_contents,
        tool_call_id=tool_call_id, message=message, surface=surface,
    )
    if _main_loop is not None and _main_loop.is_running():
        _main_loop.call_soon_threadsafe(_capture_queue.put_nowait, request)
    else:
        try:
            _capture_queue.put_nowait(request)
        except asyncio.QueueFull:
            _artifact_logger.warning("capture queue full; dropped a capture for %s", context_id)


async def _capture_worker() -> None:
    assert _capture_queue is not None
    while True:
        request = await _capture_queue.get()
        try:
            await asyncio.to_thread(_run_capture, request)
        except Exception:
            _artifact_logger.exception("artifact capture failed for %s", request.context_id)
        finally:
            _capture_queue.task_done()


def _project_id_for_context(context_id: str) -> str:
    if _session_factory is None:
        return ""
    session = _session_factory()
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
    now = datetime.now(timezone.utc).isoformat()
    version_id = str(uuid.uuid4())
    with sqlite_write_lock():
        session = _session_factory()
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
        session = _session_factory()
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
    if _session_factory is None:
        return []
    database_session = _session_factory()
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
    if _session_factory is None:
        return []
    database_session = _session_factory()
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
    if _session_factory is None:
        return []
    database_session = _session_factory()
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
    if _session_factory is None:
        return []
    database_session = _session_factory()
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
    if _session_factory is None:
        raise HTTPException(status_code=503, detail="Database is not ready.")
    surface_id = (request.surface_id or "").strip()
    version_id = (request.version_id or "").strip()
    if not surface_id or not version_id:
        raise HTTPException(status_code=400, detail="Annotation surface_id and version_id are required.")
    updated_at = request.updated_at or datetime.now(timezone.utc).isoformat()
    key = {"context_id": context_id, "surface_id": surface_id, "version_id": version_id}
    with sqlite_write_lock():
        database_session = _session_factory()
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
    if _session_factory is None:
        return False
    key = {"context_id": context_id, "surface_id": surface_id, "version_id": version_id}
    with sqlite_write_lock():
        database_session = _session_factory()
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


def _apply_history_schema(sync_engine) -> None:
    """Make the on-disk schema match the declarative models exactly — the models (and
    their ``__table_args__`` indexes) are the single source of truth. Missing tables and
    indexes are created; any existing table whose columns have drifted from its model (an
    older dev build) is dropped and recreated fresh. There is deliberately no
    backward-compatibility migration path: with no data worth preserving across a schema
    change, "make it proper" means recreate, not hand-patch individual columns."""
    inspector = inspect(sync_engine)
    existing_tables = set(inspector.get_table_names())
    drifted_tables = []
    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing_tables:
            continue
        model_columns = {column.name for column in table.columns}
        live_columns = {column["name"] for column in inspector.get_columns(table_name)}
        if model_columns != live_columns:
            drifted_tables.append(table)
    if drifted_tables:
        with sync_engine.begin() as connection:
            for table in drifted_tables:
                connection.execute(text(f"DROP TABLE {table.name}"))
    Base.metadata.create_all(sync_engine)

class SessionTitle(BaseModel):
    """Structured schema returned by the title-generation LLM call."""

    title: str = Field(
        description=(
            "A concise imperative phrase starting with a verb, then the action it describes; "
            "normal sentence case (not Title Case), no surrounding quotes, no trailing punctuation."
        ),
    )


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
    assert _global_configuration is not None
    configuration = _global_configuration
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
        _broadcaster.publish({"type": "sessions_changed"})


def _set_session_title(context_id: str, title: str) -> bool:
    assert _session_factory is not None
    with sqlite_write_lock():
        database_session = _session_factory()
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
    assert _session_factory is not None
    database_session = _session_factory()
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
                        _file_lease_manager.active_for_session(row.id)
                        if _file_lease_manager is not None
                        else []
                    ),
                    "running": row.id in _running_contexts,
                    "awaiting_input": any(
                        request_id.startswith(f"perm-{row.id}-") and not future.done()
                        for request_id, future in _pending_permissions.items()
                    ),
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
    assert _global_configuration is not None
    configuration = load_agent_configuration(agent_name, _global_configuration.agent_directories())
    skill_roots = (
        _global_configuration.skill_directories_for(working_directory)
        if working_directory
        else _global_configuration.skill_directories()
    )
    all_skills = load_skills(skill_roots)
    agent_skills = skills_for_agent(all_skills, configuration.skills)
    return configuration, build_agent_card(configuration, agent_skills, PUBLIC_BASE_URL)


def _mount_agent(application: FastAPI, agent_name: str) -> None:
    """Serve one agent profile as its own A2A endpoint: its own executor, request
    handler, and AgentCard, mounted at a per-agent path. Idempotent."""
    assert _global_configuration is not None and _task_store is not None and _registry is not None
    _configuration, card = _card_for(agent_name)
    if agent_name in _mounted_agents:
        _registry.register(agent_name, _registry._handlers[agent_name], card)
        return
    executor = HarnessAgentExecutor(
        agent_name=agent_name,
        global_configuration=_global_configuration,
        task_store=_task_store,
        pending_permissions=_pending_permissions,
        pending_questions=_pending_questions,
        registry=_registry,
        on_new_context=_record_session_visible,
        conversations=_conversations,
        on_turn_state=_set_turn_state,
        on_permission_state=_notify_permission_state,
        load_conversation=_load_conversation,
        save_conversation=_save_conversation,
        session_permission_mode_for=_session_permission_mode_for,
        on_stream_event=_publish_stream_event,
        file_lease_manager=_file_lease_manager,
        ensure_session_workspace=_ensure_session_workspace,
        ensure_mcp_servers=_ensure_mcp_servers_for,
        resolve_locations=_resolve_session_locations,
        capture_artifacts=_capture_artifacts,
    )
    handler = DefaultRequestHandler(agent_executor=executor, task_store=_task_store)
    _executors[agent_name] = executor
    _registry.register(agent_name, handler, card)
    rpc_path = agent_rpc_path(agent_name)
    A2AFastAPIApplication(agent_card=card, http_handler=handler).add_routes_to_app(
        application,
        rpc_url=rpc_path,
        agent_card_url=f"{rpc_path}{AGENT_CARD_PATH}",
    )
    _mounted_agents.add(agent_name)


def _ensure_agents_for(working_directory: str) -> None:
    """Mount any agent the working directory declares that isn't mounted yet, so a
    folder's project-local agents become addressable A2A routes once that folder is
    selected. The route pool is shared and only grows — nothing is unmounted."""
    assert _global_configuration is not None
    directories = (
        _global_configuration.agent_directories_for(working_directory)
        if working_directory
        else _global_configuration.agent_directories()
    )
    for agent_name in list_agent_route_names(directories):
        if agent_name not in _mounted_agents:
            _mount_agent(app, agent_name)


def _agent_directories_for_request(working_directory: str) -> list[Path]:
    assert _global_configuration is not None
    return (
        _global_configuration.agent_directories_for(working_directory)
        if working_directory
        else _global_configuration.agent_directories()
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


def _string_keyed_mapping(value: Any) -> dict[str, Any]:
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else {}


def _apply_agent_configuration_update(sidecar: dict[str, Any], request: "AgentConfigurationUpdateRequest") -> dict[str, Any]:
    next_sidecar = dict(sidecar)
    if request.model is not None or request.provider is not None or request.reasoning_effort is not None:
        preset = _string_keyed_mapping(next_sidecar.get("preset"))
        if request.model is not None:
            preset["model"] = request.model
        if request.provider is not None:
            preset["provider"] = request.provider
        if request.reasoning_effort is not None:
            preset["reasoningEffort"] = request.reasoning_effort
        next_sidecar["preset"] = preset
    if request.permission_mode is not None:
        next_sidecar["permissionMode"] = request.permission_mode
    if request.stream_agent_progress is not None:
        next_sidecar["streamAgentProgress"] = request.stream_agent_progress

    tools = _string_keyed_mapping(next_sidecar.get("tools"))
    if request.tools_enabled is not None:
        tools["enabledBuiltinTools"] = request.tools_enabled
    if request.bash is not None:
        bash = _string_keyed_mapping(tools.get("bash"))
        if request.bash.enabled is not None:
            bash["enabled"] = request.bash.enabled
        if request.bash.background_allowed is not None:
            bash["backgroundAllowed"] = request.bash.background_allowed
        if request.bash.permissions is not None:
            bash["permissions"] = {
                pattern.strip(): decision.strip().lower()
                for pattern, decision in request.bash.permissions.items()
                if pattern.strip() and decision.strip().lower() in {"allow", "ask", "deny"}
            }
        tools["bash"] = bash
    if request.spawn_agent is not None:
        spawn_agent = _string_keyed_mapping(tools.get("spawnAgent"))
        if request.spawn_agent.enabled is not None:
            spawn_agent["enabled"] = request.spawn_agent.enabled
        tools["spawnAgent"] = spawn_agent
    next_sidecar["tools"] = tools
    return next_sidecar


def _reload_agent_cards() -> None:
    """Recompile AgentCards from the agent markdown and skill files so discovery
    reflects edits without a restart. Agent behaviour itself is already live,
    since each turn loads its configuration and skills fresh."""
    assert _global_configuration is not None and _registry is not None
    for agent_name in list_agent_route_names(_global_configuration.agent_directories()):
        handler = _registry._handlers.get(agent_name)
        if handler is not None:
            _configuration, card = _card_for(agent_name)
            _registry.register(agent_name, handler, card)


def _watched_a2a_paths() -> list[str]:
    assert _global_configuration is not None
    directories = [
        # The .agents roots are watched recursively so mcp.json (live MCP server
        # definitions) is picked up alongside the agents/ and skills/ subtrees.
        *_global_configuration.agents_root_directories(),
        *_global_configuration.agent_directories(),
        *_global_configuration.skill_directories(),
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
    global _mcp_manager
    assert _global_configuration is not None
    # Serialize with the settings endpoints and the configuration watcher: they all
    # rebuild the shared `_mcp_manager`, so overlapping runs would clobber it.
    async with _configuration_lock:
        _global_configuration.mcp = GlobalConfiguration.load().mcp
        # Re-fold the startup-provisioned Composio server back in so a live mcp.json
        # edit doesn't drop Composio's tools (and the agent keeps its MCP tools).
        _global_configuration.mcp.servers.update(_composio_servers)
        enabled = _global_configuration.mcp.enabled_servers()
        if _mcp_manager is None:
            if enabled:
                _mcp_manager = MCPClientManager(enabled)
                await _mcp_manager.start()
                set_mcp_client_manager(_mcp_manager)
        else:
            await _mcp_manager.reconcile(enabled)
        for executor in _executors.values():
            executor.reset_runtimes()


async def _ensure_mcp_servers_for(working_directory: str) -> None:
    """Additively grow the shared MCP server pool with the working directory's own
    ``mcp.json`` servers, so a folder's servers are running and listable once that
    folder is selected. The pool is a union — servers are only added or updated,
    never removed — so no other session loses its servers."""
    global _mcp_manager
    assert _global_configuration is not None
    if not working_directory:
        return
    # Serialize with every other `_mcp_manager` mutator (settings save, config/mcp.json
    # watchers) so concurrent reconciles never clobber the shared manager.
    async with _configuration_lock:
        folder_servers = _global_configuration.mcp_configuration_for(working_directory).servers
        new_servers = {
            name: configuration
            for name, configuration in folder_servers.items()
            if _global_configuration.mcp.servers.get(name) != configuration
        }
        if not new_servers:
            return
        _global_configuration.mcp.servers.update(new_servers)
        enabled = _global_configuration.mcp.enabled_servers()
        if _mcp_manager is None:
            if enabled:
                _mcp_manager = MCPClientManager(enabled)
                await _mcp_manager.start()
                set_mcp_client_manager(_mcp_manager)
        else:
            await _mcp_manager.reconcile(enabled)
        for executor in _executors.values():
            executor.reset_runtimes()


async def _watch_agents_and_skills(application: FastAPI) -> None:
    """Watch the agents, skills, and mcp.json sources; on any change, mount newly
    added agents, reload MCP servers, refresh cards, and broadcast so connected
    clients refetch immediately. Agents, skills, and MCP servers are all picked up
    live, so the only thing needing a restart is a change to the core harness."""
    assert _global_configuration is not None
    watched = _watched_a2a_paths()
    if not watched:
        return
    try:
        async for changes in awatch(*watched):
            if any(str(path).endswith("mcp.json") for _change, path in changes):
                await _reload_mcp()
            for agent_name in list_agent_route_names(_global_configuration.agent_directories()):
                if agent_name not in _mounted_agents:
                    _mount_agent(application, agent_name)
            _reload_agent_cards()
            _broadcaster.publish({"type": "agents_changed"})
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
    global _composio_servers, _mcp_manager
    assert _global_configuration is not None
    configuration = _global_configuration
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
    _composio_servers = composio_mcp_servers(configuration.composio)
    if _composio_servers:
        configuration.mcp.servers.update(_composio_servers)
    else:
        configuration.mcp.servers.pop(configuration.composio.server_name, None)
    if _mcp_manager is not None:
        await _mcp_manager.aclose()
    mcp_servers = configuration.mcp.enabled_servers()
    _mcp_manager = MCPClientManager(mcp_servers) if mcp_servers else None
    if _mcp_manager is not None:
        await _mcp_manager.start()
    set_mcp_client_manager(_mcp_manager)
    for executor in _executors.values():
        executor.reset_runtimes()


# The content digest of the last configuration.yaml this process wrote, so the on-disk
# watcher can tell our own saves (skip — already applied in-process) apart from a manual
# user edit (apply). Set by every server-side configuration write via _persist_configuration.
_last_written_configuration_digest: Optional[str] = None

# Serializes every configuration mutation-and-apply (settings endpoints + the on-disk
# watcher) so they never interleave at an await point. Without it, a UI save and a
# concurrent disk-edit reload could both rebuild the MCP client manager, clobbering the
# `_mcp_manager` global and leaking a half-started manager.
_configuration_lock = asyncio.Lock()

# The in-flight ChatGPT sign-in, if any. A sign-in owns a loopback server on port
# 1455 for its lifetime, so at most one runs at a time; starting a new one supersedes
# any stale flow. Held only between /auth/chatgpt/start and the browser redirect.
_chatgpt_login_flow: Optional[ChatGPTLoginFlow] = None


def _configuration_digest() -> Optional[str]:
    """A content hash of ~/.daisy/configuration.yaml, or ``None`` if it is absent."""
    try:
        return hashlib.sha256(configuration_file_path().read_bytes()).hexdigest()
    except OSError:
        return None


async def _persist_configuration(**changes) -> None:
    """Write configuration changes to disk and remember the resulting content digest so
    the on-disk watcher does not treat our own save as an external edit."""
    global _last_written_configuration_digest
    await asyncio.to_thread(save_api_keys, **changes)
    _last_written_configuration_digest = await asyncio.to_thread(_configuration_digest)


async def _reload_configuration_from_disk() -> None:
    """Re-read ~/.daisy/configuration.yaml after a manual on-disk edit and apply it live:
    refresh the in-memory credentials/settings, rebuild the credential-dependent clients,
    and broadcast so every connected client refetches. The MCP server *set* (mcp.json plus
    any folder-added servers) is left to its own watcher — only the credential-derived
    Composio server is re-provisioned here."""
    assert _global_configuration is not None
    fresh = await asyncio.to_thread(GlobalConfiguration.load)
    configuration = _global_configuration
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
    _broadcaster.publish({"type": "settings_changed"})


async def _watch_configuration() -> None:
    """Watch ~/.daisy/configuration.yaml and mirror manual on-disk edits into the running
    server and every connected client — so editing an API key (or any setting) directly in
    the file takes effect immediately, no restart. The file is the single source of truth;
    our own writes are recognized by digest and skipped so a UI save does not echo."""
    global _last_written_configuration_digest
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
            async with _configuration_lock:
                digest = await asyncio.to_thread(_configuration_digest)
                if digest is not None and digest == _last_written_configuration_digest:
                    continue  # our own write echoing back — already applied in-process
                _last_written_configuration_digest = digest
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
            _broadcaster.publish({"type": "hosts_changed"})
    except asyncio.CancelledError:
        pass


@asynccontextmanager
async def lifespan(application: FastAPI):
    global _global_configuration, _session_factory, _async_engine, _task_store, _registry, _mcp_manager, _composio_servers, _main_loop, _file_lease_manager, _workspace_manager, _terminal_manager, _last_written_configuration_digest
    _main_loop = asyncio.get_running_loop()
    _file_lease_manager = FileLeaseManager(on_change=_notify_filesystem_lease_state)
    _workspace_manager = SessionWorkspaceManager()
    _terminal_manager = TerminalSessionManager()
    _global_configuration = GlobalConfiguration.load()
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
    _last_written_configuration_digest = _configuration_digest()

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
    _session_factory = sessionmaker(bind=sync_engine)
    # Guarantee at least one project exists so the app always opens into a live workspace
    # (no landing page, no empty state). On a fresh install this seeds the "Home" project.
    await asyncio.to_thread(_ensure_default_project)

    # Install the tool tuning policy from the loaded configuration before any tool can run.
    set_tuning(tuning_from_policy(_global_configuration.tuning))

    exa_key = _global_configuration.exa.effective_api_key
    if exa_key:
        from exa_py import Exa
        set_exa_client(Exa(api_key=exa_key))
    _rebuild_web_fetch_clients(_global_configuration)

    # Provision the Composio Tool Router (best-effort) and fold it into the MCP
    # config itself, so both the client manager and the agent's tool gating
    # (which binds list_mcp_tools/call_mcp_tool only when a server is configured)
    # see it — Composio's tools then ride the normal MCP path.
    _composio_servers = composio_mcp_servers(_global_configuration.composio)
    _global_configuration.mcp.servers.update(_composio_servers)
    mcp_servers = _global_configuration.mcp.enabled_servers()
    _mcp_manager = MCPClientManager(mcp_servers) if mcp_servers else None
    set_mcp_client_manager(_mcp_manager)
    if _mcp_manager is not None:
        # Connect MCP servers in the BACKGROUND so a slow or hung server (a cold
        # `uvx`/`npx` spawn, a stalled HTTP endpoint) can never delay — let alone block —
        # the harness boot; the app was failing to start when an MCP handshake stalled.
        # The manager is already wired (tool gating keys on it, not on live connections),
        # so each server's tools simply appear as it finishes connecting. The task is
        # pinned to the manager so it is not garbage-collected mid-run.
        _mcp_manager.start_task = asyncio.create_task(_mcp_manager.start())

    _async_engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        # Wait up to 30s for the write lock instead of raising "database is locked"
        # when the task store and UI reads contend. WAL (set above) lets reads run
        # concurrently with writes.
        connect_args={"timeout": 30},
    )

    @event.listens_for(_async_engine.sync_engine, "connect")
    def _set_async_sqlite_pragma(dbapi_connection, _connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    _task_store = AppendOnlyTaskStore(_async_engine)
    await _task_store.initialize()

    _registry = AgentRegistry(_task_store)
    for agent_name in list_agent_route_names(_global_configuration.agent_directories()):
        _mount_agent(application, agent_name)

    # Kill any process groups orphaned by a previous unclean shutdown (a SIGKILL or
    # crash could not run the teardown handlers) so a background shell subtree — a
    # dev server, a watcher — never survives across a restart. Runs before recovery
    # marks those jobs abandoned.
    await asyncio.to_thread(reap_orphaned_processes)

    # Recover background jobs persisted by a previous run: interrupted ones are
    # flagged for re-run and every context with a deliverable result is woken with
    # an autonomous turn so the agent picks the work back up on its own.
    for executor in _executors.values():
        await executor.resume_pending_on_startup()

    watcher = asyncio.create_task(_watch_agents_and_skills(application))
    configuration_watcher = asyncio.create_task(_watch_configuration())
    ssh_hosts_watcher = asyncio.create_task(_watch_ssh_hosts())
    # The artifact-capture worker drains write-ish tool calls and runs the shadow-git
    # capture off-loop, so it never blocks a turn (best-effort, see _run_capture).
    global _capture_queue
    _capture_queue = asyncio.Queue(maxsize=4096)
    capture_worker = asyncio.create_task(_capture_worker())
    try:
        yield
    finally:
        watcher.cancel()
        configuration_watcher.cancel()
        ssh_hosts_watcher.cancel()
        capture_worker.cancel()
        if _terminal_manager is not None:
            await _terminal_manager.close_all()
        cancel_all_background_tasks()
        if _mcp_manager is not None:
            await _mcp_manager.aclose()
        if _proxy_client is not None:
            await _proxy_client.aclose()


app = FastAPI(title="harness", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


@app.exception_handler(Exception)
async def _cors_exception_handler(request: Request, exc: Exception):
    """Starlette's CORSMiddleware sits inside ServerErrorMiddleware, so exceptions
    that reach the outer middleware bypass CORS entirely — the browser then blocks
    the response and the frontend sees a CORS error instead of the real status.
    Catch unhandled exceptions here and return a CORS-headed 500 so the client at
    least sees the error code."""
    import logging as _logging
    _logging.getLogger("harness.server").exception("Unhandled error in %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
        headers={"Access-Control-Allow-Origin": "*"},
    )


class AgentInfo(BaseModel):
    id: str
    name: str
    title: str = ""
    # What the agent is for — shown as the subtitle in the UI's agent picker.
    description: str = ""
    # The agent's resolved ``provider/model`` identifier, or empty when it falls
    # Empty means the agent is misconfigured; runtime model selection is per-agent.
    model: str = ""


class AgentBashConfigurationResponse(BaseModel):
    enabled: bool
    background_allowed: bool
    permissions: dict[str, str]


class AgentSpawnConfigurationResponse(BaseModel):
    enabled: bool


class AgentConfigurationResponse(BaseModel):
    id: str
    name: str
    title: str
    model: str = ""
    provider: str = ""
    reasoning_effort: str = "high"
    permission_mode: Literal["default", "auto", "read_only", "bypass"]
    stream_agent_progress: bool
    tools_enabled: list[str]
    bash: AgentBashConfigurationResponse
    spawn_agent: AgentSpawnConfigurationResponse
    path: str


class AgentBashConfigurationRequest(BaseModel):
    enabled: bool | None = None
    background_allowed: bool | None = None
    permissions: dict[str, str] | None = None


class AgentSpawnConfigurationRequest(BaseModel):
    enabled: bool | None = None


class AgentConfigurationUpdateRequest(BaseModel):
    model: str | None = None
    provider: str | None = None
    reasoning_effort: str | None = None
    permission_mode: Literal["default", "auto", "read_only", "bypass"] | None = None
    stream_agent_progress: bool | None = None
    tools_enabled: list[str] | None = None
    bash: AgentBashConfigurationRequest | None = None
    spawn_agent: AgentSpawnConfigurationRequest | None = None


class AgentsList(BaseModel):
    agents: list[AgentInfo]
    # The server's configured default agent id, so the UI can fall back to it
    # (rather than an arbitrary first entry) when a folder's agents load.
    defaultAgent: str = ""


class DirectoryValidationRequest(BaseModel):
    directory: str


class PermissionRequest(BaseModel):
    request_id: str
    decision: str


class QuestionRequest(BaseModel):
    request_id: str
    # One entry per question, in order: a list of selected labels (plus any
    # custom text the user typed). A skipped question is an empty entry. The
    # runtime returns this verbatim to the tool.
    answers: list[Any]
    # The user dismissed the whole prompt without answering. The tool reports the
    # decline to the model and the turn stops rather than proceeding on a guess.
    declined: bool = False


class SteeringRequest(BaseModel):
    message: str


class DirectoryRevealRequest(BaseModel):
    path: str


class ArtifactAnnotationSaveRequest(BaseModel):
    surface_id: str
    version_id: str  # the version's git commit sha
    annotations: list[dict[str, Any]]
    updated_at: str | None = None


class ArtifactRestoreRequest(BaseModel):
    location_uri: str = ""
    git_directory: str
    work_tree: str
    relative_path: str
    commit_sha: str


class PermissionModeRequest(BaseModel):
    mode: Literal["default", "auto", "read_only", "bypass"]


class SessionDraftRequest(BaseModel):
    input_draft: str = ""


class SettingsUpdateRequest(BaseModel):
    exa_api_key: str | None = None
    composio_api_key: str | None = None
    jina_api_key: str | None = None
    firecrawl_api_key: str | None = None
    web_fetch_proxy_url: str | None = None
    permission_mode: Literal["default", "auto", "read_only", "bypass"] | None = None
    sandbox_enabled: bool | None = None
    # Per-provider API keys (the opencode gateway's key lives under "opencode").
    provider_keys: dict[str, str] | None = None
    # Base URLs for the OpenAI-compatible providers (opencode, custom).
    provider_base_urls: dict[str, str] | None = None
    workspace_strategy: Literal["none", "branch", "worktree"] | None = None


class SandboxUpdateRequest(BaseModel):
    enabled: bool


class UserContextUpdateRequest(BaseModel):
    """Opt-in/out of the personal user-context snapshot in the system prompt."""
    enabled: bool


class ComputerControlUpdateRequest(BaseModel):
    """Opt-in/out of the computer-use tool that controls macOS apps."""
    enabled: bool


class CompactionUpdateRequest(BaseModel):
    """Observational-memory compaction settings. Only provided fields are changed."""
    auto: bool | None = None
    observer_context_fraction: float | None = None
    reflector_observation_fraction: float | None = None
    keep_recent_turns: int | None = None


class MCPToolCallRequest(BaseModel):
    server: str
    tool_name: str
    arguments: dict = {}


class MCPResourceReadRequest(BaseModel):
    server: str
    uri: str


# Projects, locations, and the SSH host registry.
#
# A project is the top-level unit of work: a named container owning a set of locations
# (local + SSH remotes). These are server-owned domain data in history.db. Hosts are
# read from ~/.ssh/config (the OS source of truth) via the system ssh.

class LocationInput(BaseModel):
    # `name` is not accepted — it is derived from the connection (see _derive_location_name).
    kind: str  # "local" | "remote"
    base_directory: str
    host_alias: str = ""
    permission_mode: str = "default"


class ProjectCreateRequest(BaseModel):
    name: str
    description: str = ""
    # A project is created with at least one location (the New Project wizard requires it).
    locations: list[LocationInput] = []


class ProjectUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None


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
        "name": record.name,
        "description": record.description or "",
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
    assert _session_factory is not None
    database_session = _session_factory()
    try:
        rows = database_session.query(ProjectRecord).order_by(ProjectRecord.updated_at.desc()).all()
        return {"projects": [_serialize_project(row, database_session) for row in rows]}
    finally:
        database_session.close()


def _project_payload(project_id: str) -> dict[str, Any] | None:
    assert _session_factory is not None
    database_session = _session_factory()
    try:
        record = database_session.get(ProjectRecord, project_id)
        return _serialize_project(record, database_session) if record is not None else None
    finally:
        database_session.close()


def _create_project(request: ProjectCreateRequest) -> dict[str, Any]:
    assert _session_factory is not None
    conflict = _locations_conflict_message([(location.kind, location.host_alias, location.base_directory) for location in request.locations])
    if conflict:
        raise ValueError(conflict)
    with sqlite_write_lock():
        database_session = _session_factory()
        try:
            now = _iso_now()
            project = ProjectRecord(
                id=str(uuid.uuid4()),
                name=request.name.strip() or "Untitled project",
                description=(request.description or "").strip(),
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
    """Guarantee at least one project exists so the app always opens straight into a live
    workspace. On a fresh install (no projects yet) this creates the default "Home" project,
    whose single local location is the server user's home directory. A no-op once any project
    exists, so it never fights a user who has organized their own projects."""
    assert _session_factory is not None
    with sqlite_write_lock():
        database_session = _session_factory()
        try:
            if database_session.query(ProjectRecord).count() > 0:
                return
            now = _iso_now()
            project = ProjectRecord(
                id=str(uuid.uuid4()),
                name="Home",
                description="Your home folder — the default workspace.",
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
    assert _session_factory is not None
    database_session = _session_factory()
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


def _update_project(project_id: str, request: ProjectUpdateRequest) -> dict[str, Any] | None:
    assert _session_factory is not None
    with sqlite_write_lock():
        database_session = _session_factory()
        try:
            project = database_session.get(ProjectRecord, project_id)
            if project is None:
                return None
            if request.name is not None:
                project.name = request.name.strip() or project.name
            if request.description is not None:
                project.description = request.description.strip()
            project.updated_at = _iso_now()
            database_session.commit()
            return _serialize_project(project, database_session)
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


def _delete_project(project_id: str) -> bool:
    """Delete a project and everything under it: its locations, its sessions, and the
    per-(session, location) worktree records. (Remote worktree teardown over SSH is a
    follow-up — the DB rows go now.)"""
    assert _session_factory is not None
    with sqlite_write_lock():
        database_session = _session_factory()
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
    assert _session_factory is not None
    with sqlite_write_lock():
        database_session = _session_factory()
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
    assert _session_factory is not None
    with sqlite_write_lock():
        database_session = _session_factory()
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
    assert _session_factory is not None
    with sqlite_write_lock():
        database_session = _session_factory()
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
    if _session_factory is None:
        return None
    database_session = _session_factory()
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
    for executor in _executors.values():
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
    assert _session_factory is not None
    database_session = _session_factory()
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
    assert _session_factory is not None
    with sqlite_write_lock():
        database_session = _session_factory()
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


def _terminal_directory(context_id: str, working_directory: str) -> Path:
    directory = working_directory.strip()
    if context_id and _session_factory is not None:
        database_session = _session_factory()
        try:
            record = database_session.get(SessionRecord, context_id)
            if record is not None:
                directory = record.runtime_working_directory or record.working_directory or directory
        finally:
            database_session.close()
    path = Path(directory or str(Path.home())).expanduser()
    if not path.is_absolute():
        path = Path.home() / path
    resolved = path.resolve(strict=False)
    if not resolved.is_dir():
        raise ValueError(f"Terminal directory does not exist: {resolved}")
    return resolved


def _shell_command() -> list[str]:
    # The user's real login shell comes from the passwd database — the same source
    # `login` uses — not from $SHELL, which reflects whatever shell happened to launch
    # this server and would be wrong on a remote/shared host.
    try:
        shell = pwd.getpwuid(os.getuid()).pw_shell
    except (KeyError, OSError):
        shell = ""
    if shell and Path(shell).exists():
        return [shell, "-l"]
    if platform.system() == "Darwin" and Path("/bin/zsh").exists():
        return ["/bin/zsh", "-l"]
    if Path("/bin/bash").exists():
        return ["/bin/bash", "-l"]
    return ["/bin/sh", "-l"]


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
def _login_base_environment() -> dict[str, str]:
    environment: dict[str, str] = {}
    try:
        record = pwd.getpwuid(os.getuid())
        environment["HOME"] = record.pw_dir
        environment["USER"] = record.pw_name
        environment["LOGNAME"] = record.pw_name
        if record.pw_shell:
            environment["SHELL"] = record.pw_shell
    except (KeyError, OSError):
        # No passwd entry (unusual): fall back to the interpreter's notion of $HOME.
        environment["HOME"] = str(Path.home())
    # The default PATH `login` seeds; the login shell and its rc files (e.g. macOS
    # path_helper, home-manager) immediately rebuild the real one on top of it.
    environment["PATH"] = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    # The PTY is an xterm-compatible emulator, so advertise it as one.
    environment["TERM"] = "xterm-256color"
    environment["COLORTERM"] = "truecolor"
    return environment


def _resize_pty(master_fd: int, rows: int, columns: int) -> None:
    safe_rows = max(1, min(int(rows or 24), 200))
    safe_columns = max(1, min(int(columns or 80), 400))
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", safe_rows, safe_columns, 0, 0))


async def _read_pty(master_fd: int) -> bytes:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[bytes] = loop.create_future()

    def on_ready() -> None:
        if future.done():
            return
        try:
            future.set_result(os.read(master_fd, 8192))
        except OSError as exception:
            future.set_exception(exception)

    loop.add_reader(master_fd, on_ready)
    try:
        return await future
    finally:
        loop.remove_reader(master_fd)


def _terminal_context_identifier(context_id: str, directory: Path) -> str:
    if context_id:
        return context_id
    digest = hashlib.sha256(str(directory).encode("utf-8")).hexdigest()[:16]
    return f"working-directory:{digest}"


def _load_terminal_state(context_identifier: str, terminal_key: str) -> str:
    if _session_factory is None:
        return ""
    database_session = _session_factory()
    try:
        record = database_session.get(TerminalStateRecord, (context_identifier, terminal_key))
        return record.scrollback if record is not None and record.scrollback else ""
    finally:
        database_session.close()


def _save_terminal_state(context_identifier: str, terminal_key: str, directory: Path, scrollback: str) -> None:
    if _session_factory is None:
        return
    with sqlite_write_lock():
        database_session = _session_factory()
        try:
            now = datetime.now(timezone.utc).isoformat()
            record = database_session.get(TerminalStateRecord, (context_identifier, terminal_key))
            if record is None:
                record = TerminalStateRecord(
                    context_id=context_identifier,
                    terminal_key=terminal_key,
                    working_directory=str(directory),
                    scrollback=scrollback,
                    created_at=now,
                    updated_at=now,
                )
                database_session.add(record)
            else:
                record.working_directory = str(directory)
                record.scrollback = scrollback
                if not record.created_at:
                    record.created_at = now
                record.updated_at = now
            database_session.commit()
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


def _list_terminal_states(context_identifier: str) -> list[dict[str, str]]:
    """Persisted terminals for a context, ordered by creation so the client can rebuild
    a stable set of tabs. Runs off the event loop (synchronous history.db read)."""
    if _session_factory is None:
        return []
    database_session = _session_factory()
    try:
        records = (
            database_session.query(TerminalStateRecord)
            .filter(TerminalStateRecord.context_id == context_identifier)
            .all()
        )
    finally:
        database_session.close()
    entries = [
        {
            "terminal_key": record.terminal_key,
            "working_directory": record.working_directory or "",
            "created_at": record.created_at or "",
            "updated_at": record.updated_at or "",
        }
        for record in records
    ]
    # Legacy rows carry an empty created_at; fall back to terminal_key so the order is
    # still deterministic rather than arbitrary.
    entries.sort(key=lambda entry: (entry["created_at"], entry["terminal_key"]))
    return entries


def _delete_terminal_state(context_identifier: str, terminal_key: str) -> None:
    if _session_factory is None:
        return
    with sqlite_write_lock():
        database_session = _session_factory()
        try:
            record = database_session.get(TerminalStateRecord, (context_identifier, terminal_key))
            if record is not None:
                database_session.delete(record)
                database_session.commit()
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


class TerminalSession:
    def __init__(
        self,
        context_identifier: str,
        terminal_key: str,
        directory: Path,
        rows: int,
        columns: int,
        persisted_scrollback: str,
        remote_host_alias: str = "",
    ):
        self.context_identifier = context_identifier
        self.terminal_key = terminal_key
        self.directory = directory
        # When set, the terminal is an interactive login shell on this remote host (over
        # multiplexed SSH), in `directory` on that machine, rather than a local PTY.
        self.remote_host_alias = remote_host_alias
        self.master_fd = -1
        self.pid = -1
        self.rows = rows
        self.columns = columns
        self.exit_code: int | None = None
        self.exit_signal: int | None = None
        self._buffer: deque[str] = deque()
        self._buffer_characters = 0
        self._maximum_buffer_characters = 200_000
        self._subscribers: set[asyncio.Queue] = set()
        self._reader_task: asyncio.Task | None = None
        self._persist_task: asyncio.Task | None = None
        self._closed = False
        if persisted_scrollback:
            self._append_scrollback(persisted_scrollback)

    @property
    def exited(self) -> bool:
        return self.exit_code is not None or self.exit_signal is not None

    @property
    def running(self) -> bool:
        return self.pid > 0 and not self.exited and not self._closed

    async def start(self) -> None:
        if self.running:
            return
        environment = _login_base_environment()
        if self.remote_host_alias:
            # Remote terminal: ssh to the host and start an interactive login shell in the
            # location's base dir. The `cd` happens on the remote (never locally).
            command = SshExecutor(self.remote_host_alias).terminal_argv(str(self.directory))
        else:
            command = _shell_command()
        pid, master_fd = pty.fork()
        if pid == 0:
            try:
                if not self.remote_host_alias:
                    os.chdir(self.directory)
                os.execvpe(command[0], command, environment)
            except BaseException:
                os._exit(127)
        self.pid = pid
        self.master_fd = master_fd
        self.exit_code = None
        self.exit_signal = None
        _resize_pty(self.master_fd, self.rows, self.columns)
        self._reader_task = asyncio.create_task(self._read_loop())

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        snapshot = self.scrollback()
        if snapshot:
            queue.put_nowait({"type": "output", "data": snapshot})
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def resize(self, rows: int, columns: int) -> None:
        self.rows = max(1, min(int(rows or 24), 200))
        self.columns = max(1, min(int(columns or 80), 400))
        if self.master_fd >= 0 and self.running:
            _resize_pty(self.master_fd, self.rows, self.columns)

    def write(self, data: str) -> None:
        if not data or self.master_fd < 0 or not self.running:
            return
        os.write(self.master_fd, data.encode("utf-8", errors="replace"))

    def scrollback(self) -> str:
        return "".join(self._buffer)

    async def close(self) -> None:
        self._closed = True
        if self._persist_task is not None:
            self._persist_task.cancel()
            await asyncio.gather(self._persist_task, return_exceptions=True)
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
        if self.pid > 0 and self.exit_code is None and self.exit_signal is None:
            with suppress(ProcessLookupError):
                os.killpg(self.pid, signal.SIGHUP)
            with suppress(Exception):
                await asyncio.to_thread(os.waitpid, self.pid, 0)
        if self.master_fd >= 0:
            with suppress(OSError):
                os.close(self.master_fd)
            self.master_fd = -1
        await self.persist()

    async def persist(self) -> None:
        await asyncio.to_thread(
            _save_terminal_state,
            self.context_identifier,
            self.terminal_key,
            self.directory,
            self.scrollback(),
        )

    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                try:
                    chunk = await _read_pty(self.master_fd)
                except OSError:
                    break
                if not chunk:
                    break
                data = chunk.decode("utf-8", errors="replace")
                self._append_scrollback(data)
                self._broadcast({"type": "output", "data": data})
                self._schedule_persist()
        finally:
            if not self._closed:
                await self._record_exit()
                self._broadcast({
                    "type": "exit",
                    "exit_code": self.exit_code,
                    "exit_signal": self.exit_signal,
                })
            await self.persist()

    async def _record_exit(self) -> None:
        if self.pid <= 0 or self.exited:
            return
        try:
            waited_pid, status = await asyncio.to_thread(os.waitpid, self.pid, os.WNOHANG)
            if waited_pid == 0:
                waited_pid, status = await asyncio.to_thread(os.waitpid, self.pid, 0)
        except ChildProcessError:
            self.exit_code = 0
            return
        if os.WIFEXITED(status):
            self.exit_code = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            self.exit_signal = os.WTERMSIG(status)
        else:
            self.exit_code = 0

    def _append_scrollback(self, data: str) -> None:
        self._buffer.append(data)
        self._buffer_characters += len(data)
        while self._buffer_characters > self._maximum_buffer_characters and self._buffer:
            removed = self._buffer.popleft()
            self._buffer_characters -= len(removed)

    def _broadcast(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    def _schedule_persist(self) -> None:
        if self._persist_task is not None and not self._persist_task.done():
            return

        async def delayed_persist() -> None:
            await asyncio.sleep(1)
            await self.persist()

        self._persist_task = asyncio.create_task(delayed_persist())


class TerminalSessionManager:
    def __init__(self):
        self._sessions: dict[tuple[str, str], TerminalSession] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        context_id: str,
        directory: Path,
        rows: int,
        columns: int,
        terminal_key: str = "main",
        remote_host_alias: str = "",
    ) -> TerminalSession:
        context_identifier = _terminal_context_identifier(context_id, directory)
        key = (context_identifier, terminal_key)
        async with self._lock:
            existing = self._sessions.get(key)
            if existing is not None and existing.running:
                existing.resize(rows, columns)
                return existing
            persisted_scrollback = await asyncio.to_thread(_load_terminal_state, context_identifier, terminal_key)
            session = TerminalSession(context_identifier, terminal_key, directory, rows, columns, persisted_scrollback, remote_host_alias=remote_host_alias)
            self._sessions[key] = session
            await session.start()
            return session

    def live_keys(self, context_identifier: str) -> set[str]:
        return {
            terminal_key
            for (identifier, terminal_key), session in self._sessions.items()
            if identifier == context_identifier and session.running
        }

    async def close_one(self, context_identifier: str, terminal_key: str) -> None:
        key = (context_identifier, terminal_key)
        async with self._lock:
            session = self._sessions.pop(key, None)
        if session is not None:
            await session.close()

    async def close_all(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)


async def _terminal_context_for_request(context_id: str, working_directory: str) -> str:
    """Resolve the identifier a context's terminals are stored under. For a persisted
    session the identifier is the context id itself, so we can answer even when the
    working directory no longer exists; otherwise it is derived from the resolved
    directory (which must exist)."""
    try:
        directory = await asyncio.to_thread(_terminal_directory, context_id, working_directory)
    except ValueError:
        if context_id:
            return context_id
        raise HTTPException(status_code=400, detail="Terminal directory does not exist.")
    return _terminal_context_identifier(context_id, directory)


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


class AttachmentReference(BaseModel):
    """A local file the user attached by its real OS path (the Tauri desktop app
    hands over the path; the sandboxed web build cannot and falls back to /uploads)."""

    path: str


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
_proxy_client: Optional[httpx.AsyncClient] = None


def _get_proxy_client() -> httpx.AsyncClient:
    global _proxy_client
    if _proxy_client is None:
        _proxy_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            headers=_PROXY_BROWSER_HEADERS,
        )
    return _proxy_client

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
    """Delete an orphaned upload file (and its now-empty legacy per-upload directory).
    Guarded to only ever touch paths inside the uploads root, so a mis-parse can't reach
    outside it. Runs off the event loop (sync FS)."""
    try:
        path = Path(path_string)
        root = Path(uploads_root)
        if root not in path.parents:
            return
        path.unlink(missing_ok=True)
        parent = path.parent
        if parent != root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass


def _prune_session_artifacts(context_id: str) -> None:
    """Retention on session delete: drop this session's branch in every shadow repo it
    touched, then delete its artifact index rows (versions/files/surfaces/annotations) and
    its conversation. Runs off the event loop. Best-effort per repo — a missing/unreachable
    location's branch is left, but its DB rows are still cleared."""
    if _session_factory is None:
        return
    database_session = _session_factory()
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
        database_session = _session_factory()
        try:
            for model in (ArtifactVersionRecord, ArtifactFileRecord, ArtifactSurfaceRecord, ArtifactAnnotationRecord, ConversationRecord):
                identifier = ConversationRecord.context_id if model is ConversationRecord else model.context_id
                database_session.query(model).filter(identifier == context_id).delete(synchronize_session=False)
            database_session.commit()
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()


# --- Route modules -------------------------------------------------------------
# Registered here, after every shared singleton and helper above is defined, so the
# route modules can import them from this module without a half-initialized cycle.
# Register the split route modules WITHOUT binding their names into this module's
# namespace: several of them (notably `artifacts`) collide with module-level aliases used
# at runtime — e.g. `from harness.core import artifact_versioning as artifacts` — and a bare
# `from .routes import artifacts` would shadow that alias, breaking artifact capture with an
# AttributeError. Import each router module by path and include only its `router`.
import importlib as _importlib  # noqa: E402
for _route_name in ("agents", "artifacts", "chat", "filesystem", "mcp", "projects", "sessions", "settings", "terminals", "uploads"):
    app.include_router(_importlib.import_module(f"harness.server.routes.{_route_name}").router)


def run_server(host: str = "127.0.0.1", port: int = 8822):
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8822)
