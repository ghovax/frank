import asyncio
import hashlib
import json
import platform
import re
import shutil
import subprocess

import httpx
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import quote, urljoin, urlparse

import websockets
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from sqlalchemy import Boolean, Column, String, Text, create_engine, event, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sse_starlette.sse import EventSourceResponse
from watchfiles import awatch
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, messages_from_dict, messages_to_dict
from pydantic import BaseModel, Field, SecretStr

from a2a.server.apps.jsonrpc import A2AFastAPIApplication
from a2a.server.request_handlers import DefaultRequestHandler

from harness.core.a2a_executor import (
    AgentRegistry,
    HarnessAgentExecutor,
    agent_rpc_path,
    build_agent_card,
)
from harness.core.task_store import AppendOnlyTaskStore
import harness.core.configuration as _configuration
from harness.core.configuration import (
    GlobalConfiguration,
    PromptLoader,
    database_file_path,
    harness_home_directory,
    list_agent_route_names,
    list_agents,
    load_agent_configuration,
    save_api_keys,
)
from harness.core.composio_router import composio_mcp_servers
from harness.core.litellm_model import ChatLiteLLMModel
from harness.core.mcp_client import MCPClientManager
from harness.core.models import MODELS, available_models, find_model, provider_and_suffix, resolve_litellm
from harness.core.providers import PROVIDERS
from harness.core.file_leases import FileLeaseManager
from harness.core.session_workspaces import SessionWorkspace, SessionWorkspaceManager
from harness.core.sqlite_lock import configure_sqlite_lock, sqlite_write_lock
from harness.core.skills import load_skills, skills_for_agent
from harness.tools.tools import (
    ASSETS_DIRECTORY,
    cancel_all_background_tasks,
    set_exa_client,
    set_mcp_client_manager,
    _inject_widget_runtime,
)

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
    # Per-session model override (provider/model id); empty means use the global
    # selected model. Persisted so resuming a session keeps its chosen model.
    model = Column(Text, default="")
    # Per-session permission mode for future turns and frontend hydration.
    permission_mode = Column(Text, default="default")
    created_at = Column(String, nullable=False)


class ProjectHistoryRecord(Base):
    """Recent working directories selected in the UI."""

    __tablename__ = "project_history"

    path = Column(Text, primary_key=True)
    name = Column(Text, default="")
    selected_at = Column(String, nullable=False)


class ModelHistoryRecord(Base):
    """Recently selected models (provider/model id + label), mirroring the project
    history so a user can quickly switch back to a model they used before."""

    __tablename__ = "model_history"

    model_id = Column(Text, primary_key=True)
    name = Column(Text, default="")
    provider = Column(Text, default="")
    selected_at = Column(String, nullable=False)


class ConversationRecord(Base):
    """The agent's dialogue history (LangChain messages) per A2A context, persisted
    so a session keeps its context across a server restart. The A2A task store holds
    the transcript the UI replays; this holds the model-facing message list the agent
    actually resumes from."""

    __tablename__ = "conversations"

    context_id = Column(String, primary_key=True)  # == A2A contextId
    messages = Column(Text, default="")  # JSON: langchain messages_to_dict
    updated_at = Column(String, nullable=False)


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


def _session_model_for(context_id: str) -> str:
    """Read a context's persisted per-session model override (``""`` = global
    default). Used when building a runtime so a conversation runs on the model the
    user picked for it."""
    if _session_factory is None or not context_id:
        return ""
    database_session = _session_factory()
    try:
        record = database_session.get(SessionRecord, context_id)
        return (record.model or "") if record is not None else ""
    except Exception:
        return ""
    finally:
        database_session.close()


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


def _set_session_model(context_id: str, model_identifier: str) -> bool:
    """Persist a per-session model override and drop the cached runtime so the next
    turn rebuilds with the new model. Returns whether the session was found."""
    if _session_factory is None or not context_id:
        return False
    updated = False
    with sqlite_write_lock():
        database_session = _session_factory()
        try:
            record = database_session.get(SessionRecord, context_id)
            if record is None:
                return False
            record.model = model_identifier or ""
            database_session.commit()
            updated = True
        except Exception:
            database_session.rollback()
        finally:
            database_session.close()
    if updated:
        for executor in _executors.values():
            executor.reset_runtime(context_id)
    return updated


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
    source = record.working_directory or ""
    runtime = record.runtime_working_directory or source
    return SessionWorkspace(
        source_working_directory=source,
        runtime_working_directory=runtime,
        strategy=(record.workspace_strategy or "none"),
        workspace_path=record.workspace_path or "",
        workspace_branch=record.workspace_branch or "",
        source_repository_root=record.source_repository_root or "",
        runtime_repository_root=record.runtime_repository_root or "",
        head=record.workspace_head or "",
        error=record.workspace_error or "",
    )


def _record_session_visible(context_id: str) -> None:
    _publish_broadcast({"type": "sessions_changed"})


def _ensure_session_workspace(
    context_id: str,
    agent: str,
    working_directory: str,
    workspace_strategy: str,
    permission_mode: str,
    model_identifier: str,
    first_message: str,
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
    strategy = requested_strategy or (_global_configuration.workspace.strategy if _global_configuration is not None else "none")
    requested_model = (model_identifier or "").strip()
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
                working_directory=workspace.source_working_directory,
                runtime_working_directory=workspace.runtime_working_directory,
                workspace_strategy=workspace.strategy,
                workspace_path=workspace.workspace_path,
                workspace_branch=workspace.workspace_branch,
                source_repository_root=workspace.source_repository_root,
                runtime_repository_root=workspace.runtime_repository_root,
                workspace_head=workspace.head,
                workspace_error=workspace.error,
                model=requested_model,
                permission_mode=_normalize_permission_mode(permission_mode),
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
    if requested_model:
        _record_model_selection(requested_model)
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


def _record_project_path(path_value: str) -> str | None:
    directory = path_value.strip()
    if not directory:
        return None
    path = Path(directory).expanduser()
    if not path.is_absolute() or not path.exists() or not path.is_dir():
        return None

    resolved = str(path)
    assert _session_factory is not None
    with sqlite_write_lock():
        database_session = _session_factory()
        try:
            record = database_session.get(ProjectHistoryRecord, resolved)
            selected_at = datetime.now(timezone.utc).isoformat()
            if record is None:
                database_session.add(ProjectHistoryRecord(
                    path=resolved,
                    name=_project_name(resolved),
                    selected_at=selected_at,
                ))
            else:
                record.name = _project_name(resolved)
                record.selected_at = selected_at
            database_session.commit()
        except Exception:
            database_session.rollback()
            raise
        finally:
            database_session.close()
    return resolved


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
    provider, suffix = split if split is not None else (definition.provider, definition.identifier.split("/", 1)[1])
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


def _ensure_session_schema(sync_engine) -> None:
    """SQLAlchemy create_all does not add columns to an existing SQLite table."""
    inspector = inspect(sync_engine)
    if "sessions" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("sessions")}
    additions = {
        "model": "TEXT DEFAULT ''",
        "permission_mode": "TEXT DEFAULT 'default'",
        "runtime_working_directory": "TEXT DEFAULT ''",
        "workspace_strategy": "TEXT DEFAULT 'none'",
        "workspace_path": "TEXT DEFAULT ''",
        "workspace_branch": "TEXT DEFAULT ''",
        "source_repository_root": "TEXT DEFAULT ''",
        "runtime_repository_root": "TEXT DEFAULT ''",
        "workspace_head": "TEXT DEFAULT ''",
        "workspace_error": "TEXT DEFAULT ''",
    }
    missing = [(name, definition) for name, definition in additions.items() if name not in existing]
    if not missing:
        return
    with sync_engine.begin() as connection:
        for name, definition in missing:
            connection.execute(text(f"ALTER TABLE sessions ADD COLUMN {name} {definition}"))


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


async def _generate_session_title(first_message: str) -> str:
    """Ask the configured LLM for a short, structured title for the session.

    ``SessionTitle`` is bound as a tool with auto tool-choice rather than via
    ``with_structured_output``: the configured reasoning model rejects both
    ``response_format`` (json_schema) and the forced ``tool_choice`` that
    ``with_structured_output`` relies on, but accepts a regular tool call — the
    same pattern the main agent uses with ``bind_tools``.
    """
    assert _global_configuration is not None
    configuration = _global_configuration
    model_identifier = configuration.selected_model_identifier()
    resolved = resolve_litellm(
        model_identifier,
        configuration.configured_provider_keys(),
        configuration.configured_provider_bases(),
    )
    if not resolved["api_key"]:
        return ""
    llm = ChatLiteLLMModel(
        model=resolved["model"],
        api_key=SecretStr(resolved["api_key"]),
        api_base=resolved["api_base"] or None,
        temperature=0,
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
        title = await _generate_session_title(first_message)
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


def _recent_projects_payload() -> dict[str, list[dict[str, str]]]:
    """Recent working directories selected in the UI or used by sessions."""
    assert _session_factory is not None
    database_session = _session_factory()
    try:
        projects: dict[str, dict[str, str]] = {}
        project_rows = database_session.query(ProjectHistoryRecord).order_by(ProjectHistoryRecord.selected_at.desc()).all()
        for row in project_rows:
            if row.path:
                projects[row.path] = {
                    "path": row.path,
                    "name": row.name or _project_name(row.path),
                    "last_used_at": row.selected_at,
                }

        session_rows = database_session.query(SessionRecord).order_by(SessionRecord.created_at.desc()).all()
        for row in session_rows:
            path = (row.working_directory or "").strip()
            if not path:
                continue
            existing = projects.get(path)
            if existing is None or row.created_at > existing["last_used_at"]:
                projects[path] = {
                    "path": path,
                    "name": _project_name(path),
                    "last_used_at": row.created_at,
                }

        return {
            "projects": sorted(projects.values(), key=lambda project: project["last_used_at"], reverse=True)
        }
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
                    "agent": row.agent,
                    "title": row.title,
                    "created_at": row.created_at,
                    "working_directory": row.working_directory,
                    "working_directory_name": _project_name(row.working_directory) if row.working_directory else "",
                    "runtime_working_directory": row.runtime_working_directory or row.working_directory,
                    "runtime_working_directory_name": (
                        _project_name(row.runtime_working_directory)
                        if row.runtime_working_directory
                        else _project_name(row.working_directory) if row.working_directory else ""
                    ),
                    "workspace_strategy": row.workspace_strategy or "none",
                    "workspace_path": row.workspace_path or "",
                    "workspace_branch": row.workspace_branch or "",
                    "source_repository_root": row.source_repository_root or "",
                    "runtime_repository_root": row.runtime_repository_root or "",
                    "workspace_head": row.workspace_head or "",
                    "workspace_error": row.workspace_error or "",
                    "model": row.model or "",
                    "permission_mode": _normalize_permission_mode(row.permission_mode or "default"),
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
        session_model_for=_session_model_for,
        session_permission_mode_for=_session_permission_mode_for,
        on_stream_event=_publish_stream_event,
        file_lease_manager=_file_lease_manager,
        ensure_session_workspace=_ensure_session_workspace,
        ensure_mcp_servers=_ensure_mcp_servers_for,
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


@asynccontextmanager
async def lifespan(application: FastAPI):
    global _global_configuration, _session_factory, _async_engine, _task_store, _registry, _mcp_manager, _composio_servers, _main_loop, _file_lease_manager, _workspace_manager
    _main_loop = asyncio.get_running_loop()
    _file_lease_manager = FileLeaseManager(on_change=_notify_filesystem_lease_state)
    _workspace_manager = SessionWorkspaceManager()
    _global_configuration = GlobalConfiguration.load()

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

    with sqlite_write_lock():
        Base.metadata.create_all(sync_engine)
        _ensure_session_schema(sync_engine)
    _session_factory = sessionmaker(bind=sync_engine)

    exa_key = _global_configuration.exa.effective_api_key
    if exa_key:
        from exa_py import Exa
        set_exa_client(Exa(api_key=exa_key))

    # Provision the Composio Tool Router (best-effort) and fold it into the MCP
    # config itself, so both the client manager and the agent's tool gating
    # (which binds list_mcp_tools/call_mcp_tool only when a server is configured)
    # see it — Composio's tools then ride the normal MCP path.
    _composio_servers = composio_mcp_servers(_global_configuration.composio)
    _global_configuration.mcp.servers.update(_composio_servers)
    mcp_servers = _global_configuration.mcp.enabled_servers()
    _mcp_manager = MCPClientManager(mcp_servers) if mcp_servers else None
    if _mcp_manager is not None:
        await _mcp_manager.start()
    set_mcp_client_manager(_mcp_manager)

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

    # Recover background jobs persisted by a previous run: interrupted ones are
    # flagged for re-run and every context with a deliverable result is woken with
    # an autonomous turn so the agent picks the work back up on its own.
    for executor in _executors.values():
        await executor.resume_pending_on_startup()

    watcher = asyncio.create_task(_watch_agents_and_skills(application))
    try:
        yield
    finally:
        watcher.cancel()
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


class AgentsList(BaseModel):
    agents: list[AgentInfo]


class DirectoryValidationRequest(BaseModel):
    directory: str


class RecentProjectRequest(BaseModel):
    path: str


class PermissionRequest(BaseModel):
    request_id: str
    decision: str


class QuestionRequest(BaseModel):
    request_id: str
    # One entry per question, in order: a list of selected labels (plus any
    # custom text the user typed). The runtime returns this verbatim to the tool.
    answers: list[Any]


class SteeringRequest(BaseModel):
    message: str


class PermissionModeRequest(BaseModel):
    mode: Literal["default", "auto", "read_only", "bypass"]


class SettingsUpdateRequest(BaseModel):
    exa_api_key: str = ""
    composio_api_key: str = ""
    # Per-provider API keys (the opencode gateway's key lives under "opencode").
    provider_keys: dict[str, str] = {}
    # Base URLs for the OpenAI-compatible providers (opencode, custom).
    provider_base_urls: dict[str, str] = {}
    # The selected model id (provider/model) used when a session has no override.
    selected_model: str = ""
    workspace_strategy: Literal["none", "branch", "worktree"] = "none"


class SandboxUpdateRequest(BaseModel):
    enabled: bool


class MCPToolCallRequest(BaseModel):
    server: str
    tool_name: str
    arguments: dict = {}


class MCPResourceReadRequest(BaseModel):
    server: str
    uri: str


@app.get("/agents")
async def agents(working_directory: str = ""):
    """List agent profiles for the UI selector, scoped to the selected folder:
    the home globals plus that folder's own ``.agents/agents`` (deduped), never
    the directory the server was launched in. Passing ``working_directory`` is
    what makes the list track the chosen folder."""
    assert _global_configuration is not None
    if working_directory:
        _ensure_agents_for(working_directory)
        directories = _global_configuration.agent_directories_for(working_directory)
    else:
        directories = _global_configuration.agent_directories()
    agent_data = list_agents(directories)
    return AgentsList(agents=[AgentInfo(id=agent["id"], name=agent["name"], title=agent.get("title", agent["name"])) for agent in agent_data])


@app.get("/agents/cards")
async def agent_cards(working_directory: str = ""):
    """Discovery: the full A2A AgentCard for every served agent, including their
    skills, so the UI can broadcast what each agent can do.

    Skills are scoped to ``working_directory`` when given: the home globals plus
    that path's own ``.agents`` skills (deduped), and crucially *not* the skills of
    the directory the server happens to have been launched in. The UI passes the
    selected project path so the advertised skills match what a session there can
    actually find, refreshing whenever the user picks a different folder."""
    assert _registry is not None and _global_configuration is not None
    skill_roots = (
        _global_configuration.skill_directories_for(working_directory)
        if working_directory
        else _global_configuration.skill_directories()
    )
    all_skills = load_skills(skill_roots)
    skill_titles = {skill.identifier: skill.display_title for skill in all_skills}
    skill_enabled = {skill.identifier: skill.enabled for skill in all_skills}
    # Cards are served from the shared (union) route pool, but listed only for the
    # agents the selected folder actually declares (home globals plus that folder's
    # own), so the launch directory's agents don't leak into an unrelated folder.
    allowed_agents: set[str] | None = None
    if working_directory:
        _ensure_agents_for(working_directory)
        allowed_agents = {
            agent["id"]
            for agent in list_agents(_global_configuration.agent_directories_for(working_directory))
        }
    cards: list[dict] = []
    for existing in _registry.cards():
        agent_name = str(existing.name or "")
        if allowed_agents is not None and agent_name not in allowed_agents:
            continue
        try:
            configuration, card = _card_for(agent_name, working_directory)
            title = configuration.display_name
        except Exception:
            card, title = existing, agent_name
        dumped = card.model_dump(by_alias=True, exclude_none=True, mode="json")
        dumped["title"] = title
        for skill in dumped.get("skills", []):
            if isinstance(skill, dict):
                skill_name = str(skill.get("name") or skill.get("id") or "")
                skill["title"] = skill_titles.get(skill_name, skill_name)
                skill["enabled"] = skill_enabled.get(skill_name, True)
        cards.append(dumped)
    return {"cards": cards}


@app.get("/skills")
async def skills(working_directory: str = ""):
    """List the skills available in the selected folder — home globals plus that
    folder's own ``.agents/skills`` (deduped), never the launch directory. This is
    independent of any agent, so the UI can show a folder's skills even when it has
    no agents. Disabled skills are returned (flagged) so the UI greys them out."""
    assert _global_configuration is not None
    roots = (
        _global_configuration.skill_directories_for(working_directory)
        if working_directory
        else _global_configuration.skill_directories()
    )
    all_skills = load_skills(roots)
    home_root = _global_configuration.home_agents_root().resolve()
    return {
        "skills": [
            {
                "id": skill.identifier,
                "name": skill.identifier,
                "title": skill.display_title,
                "description": skill.description,
                "enabled": skill.enabled,
                "scope": _path_scope(skill.path, home_root),
            }
            for skill in all_skills
        ]
    }


@app.get(AGENT_CARD_PATH)
async def default_agent_card():
    """Serve the default agent's card at the well-known path for spec compliance."""
    assert _registry is not None and _global_configuration is not None
    card = _registry.card(_global_configuration.default_agent) or (
        _registry.cards()[0] if _registry.cards() else None
    )
    if card is None:
        return {}
    return card.model_dump(by_alias=True, exclude_none=True, mode="json")


@app.get("/home")
async def home_directory():
    """The server user's home directory and its folder name — the default project
    the UI selects before anything else is chosen."""
    home = str(Path.home())
    return {"path": home, "name": _project_name(home)}


@app.get("/projects/recent")
async def recent_projects():
    return await asyncio.to_thread(_recent_projects_payload)


@app.post("/projects/recent")
async def record_recent_project(request: RecentProjectRequest):
    """Record a validated working directory selection."""
    path = await asyncio.to_thread(_record_project_path, request.path)
    if not path:
        return {"saved": False}
    _broadcaster.publish({"type": "projects_changed"})
    return {"saved": True, "path": path, "name": _project_name(path)}


@app.get("/models")
async def list_models_endpoint():
    """The model catalog for the picker: every known model with its provider and
    whether its provider has a resolvable credential (available), plus the provider
    registry and the currently selected model. Available models are fronted in the
    picker; locked ones stay listed (greyed) so the user sees what a key unlocks."""
    assert _global_configuration is not None
    configured_keys = _global_configuration.configured_provider_keys()
    available_identifiers = {model.identifier for model in available_models(configured_keys)}
    models = [
        {
            "id": model.identifier,
            "name": model.name,
            "provider": model.provider,
            "available": model.identifier in available_identifiers,
            "curated": model.curated,
        }
        for model in MODELS
    ]
    providers = [
        {
            "id": provider.identifier,
            "name": provider.name,
            "openai_compatible": provider.openai_compatible,
        }
        for provider in PROVIDERS.values()
    ]
    selected_model = _global_configuration.selected_model_identifier()
    return {
        "models": models,
        "providers": providers,
        "selected_model": selected_model,
    }


@app.get("/models/recent")
async def recent_models():
    """Recently selected models (newest first), mirroring the project history — a
    user can quickly switch back to a model they used before without scrolling the
    full catalog. Each entry is only recorded once it is actually selected."""
    return {"models": await asyncio.to_thread(_recent_models)}


@app.get("/settings")
async def get_settings():
    """Return the API credentials stored in ~/.daisy/configuration.yaml so the
    settings dialog can pre-fill them, including per-provider keys and the selected
    model."""
    assert _global_configuration is not None
    return {
        "exa_api_key": _global_configuration.exa.api_key,
        "composio_api_key": _global_configuration.composio.api_key,
        "sandbox_enabled": _global_configuration.sandbox.enabled,
        "workspace_strategy": _global_configuration.workspace.strategy,
        "selected_model": _global_configuration.selected_model_identifier(),
        "providers": {
            identifier: {"api_key": credential.api_key, "base_url": credential.base_url}
            for identifier, credential in _global_configuration.providers.items()
        },
    }


@app.post("/settings")
async def update_settings(request: SettingsUpdateRequest):
    """Persist API credentials to ~/.daisy/configuration.yaml and apply them
    live: refresh the in-memory configuration, the Exa client, restart the MCP
    client manager so Composio tools appear/disappear with its key, and drop
    cached agent runtimes so the next turn rebuilds with the new credentials."""
    global _composio_servers, _mcp_manager
    assert _global_configuration is not None
    configuration = _global_configuration
    await asyncio.to_thread(
        save_api_keys,
        exa_api_key=request.exa_api_key,
        composio_api_key=request.composio_api_key,
        provider_keys=request.provider_keys,
        provider_base_urls=request.provider_base_urls,
        selected_model=request.selected_model,
        workspace_strategy=request.workspace_strategy,
    )
    configuration.exa.api_key = request.exa_api_key
    configuration.composio.api_key = request.composio_api_key
    configuration.workspace.strategy = request.workspace_strategy
    # Rebuild the providers map from the posted keys/base URLs, merging so a
    # provider the dialog did not render keeps its stored credential.
    merged_providers = {
        identifier: _configuration.ProviderCredential(
            api_key=credential.api_key, base_url=credential.base_url
        )
        for identifier, credential in configuration.providers.items()
    }
    for provider_identifier, api_key in request.provider_keys.items():
        existing = merged_providers.get(provider_identifier) or _configuration.ProviderCredential()
        merged_providers[provider_identifier] = existing.model_copy(update={"api_key": api_key})
    for provider_identifier, base_url in request.provider_base_urls.items():
        existing = merged_providers.get(provider_identifier) or _configuration.ProviderCredential()
        merged_providers[provider_identifier] = existing.model_copy(update={"base_url": base_url})
    configuration.providers = merged_providers
    if request.selected_model:
        # The picker carries the combined ``provider/model`` id; split it into the
        # two separate in-memory fields (mirroring how save_api_keys persists them).
        if "/" in request.selected_model:
            provider, model = request.selected_model.split("/", 1)
            configuration.selected_provider = provider
            configuration.selected_model = model
        else:
            configuration.selected_model = request.selected_model
        await asyncio.to_thread(_record_model_selection, request.selected_model)

    exa_key = configuration.exa.effective_api_key
    if exa_key:
        from exa_py import Exa
        set_exa_client(Exa(api_key=exa_key))
    else:
        set_exa_client(None)

    # Re-provision Composio now that its key may have changed: rebuild the server
    # config, fold it into (or remove it from) the MCP config, and restart the
    # MCP client manager so the agent picks Composio tools up live.
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
    _publish_broadcast({"type": "settings_changed"})
    return {"status": "saved"}


@app.post("/settings/sandbox")
async def update_sandbox(request: SandboxUpdateRequest):
    """Persist and apply the sandbox toggle independently from credentials."""
    assert _global_configuration is not None
    await asyncio.to_thread(save_api_keys, sandbox_enabled=request.enabled)
    _global_configuration.sandbox.enabled = request.enabled
    for executor in _executors.values():
        executor.reset_runtimes()
    _publish_broadcast({"type": "settings_changed"})
    return {"status": "saved", "sandbox_enabled": _global_configuration.sandbox.enabled}


@app.get("/messages/history")
async def get_message_history(working_directory: str = ""):
    """Return the last 100 user messages sent in this project, newest first."""
    if not working_directory:
        return {"messages": []}
    assert _task_store is not None
    messages = await _task_store.get_user_messages(working_directory)
    return {"messages": messages}


@app.post("/messages/history")
async def save_message_history(body: dict):
    """Persist a user message for up/down arrow recall within the project."""
    assert _task_store is not None
    await _task_store.add_user_message(body["working_directory"], body["message"])
    return {"ok": True}


@app.get("/mcp/tools")
async def mcp_tools(server: str = "", working_directory: str = ""):
    """List configured MCP servers with their enabled flag. Enabled servers carry
    their advertised tools; disabled ones are still returned (with no tools) so the
    UI can show them greyed out rather than hiding them.

    Scoped to the selected folder when ``working_directory`` is given: only the
    servers that folder declares (its own ``mcp.json`` plus the home globals) and
    the global Composio integration are listed — the launch directory's servers do
    not leak in. The folder's servers are ensured running first so their tools
    actually appear (the subprocess pool is shared and grows as a union)."""
    assert _global_configuration is not None
    # Servers declared by the working directory's own mcp.json are project-specific;
    # everything else (home globals and the Composio integration) is global.
    project_server_names: set[str] = set()
    if working_directory:
        await _ensure_mcp_servers_for(working_directory)
        allowed = set(_global_configuration.mcp_configuration_for(working_directory).servers)
        allowed.update(_composio_servers)
        configured = {
            name: configuration
            for name, configuration in _global_configuration.mcp.servers.items()
            if name in allowed
        }
        home_root = _global_configuration.home_agents_root().resolve()
        project_root = _global_configuration.project_agents_root_for(working_directory).resolve()
        if project_root != home_root:
            project_server_names = set(
                _configuration.MCPConfiguration.from_dotagents_roots([project_root]).servers
            )
    else:
        configured = _global_configuration.mcp.servers
    tools_by_server: dict[str, list] = {}
    if _mcp_manager is not None:
        # List every enabled server, then filter below — querying the manager for a
        # disabled server name would raise, since it only holds enabled ones.
        listing = await _mcp_manager.list_tools("")
        tools_by_server = {entry["name"]: entry["tools"] for entry in listing["servers"]}
    servers = [
        {
            "name": name,
            "enabled": configuration.enabled,
            "tools": tools_by_server.get(name, []),
            "scope": "project" if name in project_server_names else "global",
        }
        for name, configuration in sorted(configured.items())
        if not server or name == server
    ]
    return {"servers": servers}


@app.get("/mcp/resources")
async def mcp_resources(server: str = ""):
    """List resources exposed by configured MCP servers."""
    if _mcp_manager is None:
        return {"servers": []}
    return await _mcp_manager.list_resources(server)


@app.post("/mcp/tools/call")
async def mcp_call_tool(request: MCPToolCallRequest):
    """Call a configured MCP server tool. Intended for smoke tests and UI discovery."""
    if _mcp_manager is None:
        return {"error": "MCP is not configured."}
    return await _mcp_manager.call_tool(request.server, request.tool_name, request.arguments)


@app.post("/mcp/resources/read")
async def mcp_read_resource(request: MCPResourceReadRequest):
    """Read a configured MCP resource. Intended for smoke tests and UI discovery."""
    if _mcp_manager is None:
        return {"error": "MCP is not configured."}
    return await _mcp_manager.read_resource(request.server, request.uri)


@app.post("/directory/validate")
async def validate_directory(request: DirectoryValidationRequest):
    """Validate that a path is an existing absolute directory and report Git workspace availability."""
    # The git probes below spawn subprocesses that can block for seconds (a slow or
    # networked repository), so the whole thing runs off the event loop — a blocking
    # subprocess on the loop thread would freeze every other request until it returns.
    return await asyncio.to_thread(_validate_directory_payload, request.directory.strip())


def _validate_directory_payload(directory: str) -> dict[str, object]:
    if not directory:
        return {
            "valid": False,
            "exists": False,
            "is_directory": False,
            "is_absolute": False,
            "is_git_repository": False,
            "repository_root": "",
            "path": "",
        }
    path = Path(directory).expanduser()
    valid = path.is_absolute() and path.exists() and path.is_dir()
    is_git_repository = False
    repository_root = ""
    if valid:
        try:
            inside = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            is_git_repository = inside.returncode == 0 and inside.stdout.strip() == "true"
            if is_git_repository:
                root = subprocess.run(
                    ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if root.returncode == 0:
                    repository_root = root.stdout.strip()
        except (FileNotFoundError, subprocess.SubprocessError, subprocess.TimeoutExpired):
            is_git_repository = False
    return {
        "valid": valid,
        "exists": path.exists(),
        "is_directory": path.is_dir(),
        "is_absolute": path.is_absolute(),
        "is_git_repository": is_git_repository,
        "repository_root": repository_root,
        "path": str(path),
    }


@app.post("/directory/browse")
async def browse_directory():
    """Open a native folder picker on the local server machine and return an absolute path."""
    # The native picker blocks until the user chooses or cancels — up to five minutes.
    # It MUST run off the event loop: on the loop thread it would freeze the entire
    # server (every request hanging) for as long as the dialog stays open.
    return await asyncio.to_thread(_open_folder_picker)


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


@app.get("/sessions")
async def list_sessions():
    return await asyncio.to_thread(_sessions_payload)


@app.get("/filesystem/leases")
async def filesystem_leases():
    """Active filesystem mutation leases across all sessions in this backend."""
    return {"leases": _file_lease_manager.active() if _file_lease_manager is not None else []}


@app.get("/sessions/{context_id}/tasks")
async def session_tasks(context_id: str):
    """All A2A tasks for a context — the main turn tasks (with history and
    artifacts) plus related sub-agent tasks — for replaying a session."""
    assert _task_store is not None
    tasks = await _task_store.tasks_for_context(context_id)
    return {
        "tasks": [
            task.model_dump(by_alias=True, exclude_none=True, mode="json")
            for task in tasks
        ]
    }


@app.get("/sessions/{context_id}/tasks/page")
async def session_task_page(context_id: str, before_row_id: int | None = None, limit: int = 400):
    """A bounded replay page for fast session switching.

    Returns the newest persisted task-history rows first on the initial call;
    pass ``before_row_id`` from the previous response to load older rows. This
    keeps long conversations interactive without waiting for the complete task
    history to deserialize and cross the local HTTP boundary.
    """
    assert _task_store is not None
    page = await _task_store.task_page_for_context(context_id, before_row_id=before_row_id, limit=limit)
    return {
        "tasks": [
            task.model_dump(by_alias=True, exclude_none=True, mode="json")
            for task in page["tasks"]
        ],
        "next_before_row_id": page["next_before_row_id"],
        "has_more": page["has_more"],
    }


@app.get("/sessions/{context_id}/stream")
async def session_stream(context_id: str, request: Request):
    """Live SSE stream of a session's structured parts for a non-driving viewer.

    Emits one ``snapshot`` frame (the compacted transcript, same shape as
    /sessions/{id}/tasks) then a ``live`` tail — one frame per part the turn emits,
    in the same agent-message shape the driver's message/stream uses, so the client
    feeds them to the same reducer. Replaces per-second polling + full re-replay
    (O(N)/s) with O(delta) live updates.

    A ``done`` frame ends the stream when the turn completes (or if it already had
    by the time the viewer connected)."""
    assert _task_store is not None

    async def generate():
        # Subscribe before reading the baseline so every part published from here on
        # lands on our queue; the snapshot then covers everything up to the baseline.
        queue = _event_bus.subscribe(context_id)
        baseline = _event_bus.high_seq(context_id)
        try:
            tasks = await _task_store.tasks_for_context(context_id)
            yield {"data": json.dumps({
                "kind": "snapshot",
                "tasks": [task.model_dump(by_alias=True, exclude_none=True, mode="json") for task in tasks],
            })}

            # Drain anything queued between subscribe and now. Events with seq <=
            # baseline are already in the snapshot; only newer ones are sent live.
            done = False
            while not queue.empty():
                item = queue.get_nowait()
                if item is _ContextEventBus._DONE:
                    done = True
                    break
                seq, part = item
                if seq <= baseline:
                    continue
                yield {"data": json.dumps({"kind": "live", "seq": seq, "message": {"role": "agent", "parts": [part]}})}

            if done or _running_contexts.get(context_id, 0) == 0:
                yield {"data": json.dumps({"kind": "done"})}
                return

            # Live tail. The wait_for timeout lets us notice a client disconnect
            # promptly; the library's ping keeps the connection alive between events.
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                if item is _ContextEventBus._DONE:
                    yield {"data": json.dumps({"kind": "done"})}
                    break
                seq, part = item
                yield {"data": json.dumps({"kind": "live", "seq": seq, "message": {"role": "agent", "parts": [part]}})}
        except asyncio.CancelledError:
            raise
        finally:
            _event_bus.unsubscribe(context_id, queue)

    return EventSourceResponse(generate(), ping=15)


class SessionModelRequest(BaseModel):
    model: str


@app.put("/sessions/{context_id}/model")
async def update_session_model(context_id: str, request: SessionModelRequest):
    """Set or clear a per-session model override (provider/model id, or "" to fall
    back to the global default). Persists to the sessions table and drops the cached
    runtime so the next turn runs on the new model. A non-empty selection is also
    recorded in the model history for quick switching."""
    updated = await asyncio.to_thread(_set_session_model, context_id, request.model)
    if not updated:
        raise HTTPException(status_code=404, detail="Session not found.")
    if request.model:
        await asyncio.to_thread(_record_model_selection, request.model)
    return {"status": "saved", "model": request.model or ""}


@app.get("/sessions/{context_id}/model")
async def get_session_model(context_id: str):
    """The per-session model override for a context ("" = global default)."""
    return {"model": await asyncio.to_thread(_session_model_for, context_id)}


@app.get("/preview/{file_path:path}")
async def preview_file(file_path: str):
    """Serve a local file for an ``open_preview`` artifact (the UI points a
    sandboxed iframe here). HTML gets the widget runtime injected so a previewed
    page can self-size, report render errors, and be interactive; everything else
    (images, PDFs, CSS/JS assets a page references) is served verbatim so relative
    links inside a previewed page resolve. Localhost-only, like the rest of the API."""
    path = Path("/" + file_path.lstrip("/")).resolve()
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    suffix = path.suffix.lower()
    # Previews are live local files being iterated on, and a page's sibling assets
    # (CSS/JS/images) are fetched by their stable paths without the cache-busting
    # version the iframe URL carries — so serve everything no-store, or a refresh
    # would reload the HTML but keep showing cached assets.
    no_store = {"Cache-Control": "no-store"}
    if suffix in (".html", ".htm", ".xhtml"):
        try:
            markup = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise HTTPException(status_code=400, detail=f"Could not read file: {error}")
        return HTMLResponse(_inject_widget_runtime(markup), headers=no_store)
    return FileResponse(path, headers=no_store)


@app.post("/research/uploads")
async def upload_research_file(file: UploadFile = File(...)):
    """Store a user-provided research artifact under Daisy's managed home.

    The response is intentionally source-shaped so a model can insert it into the
    research blackboard as an `origin_channel="upload"` source without inventing
    metadata.
    """
    raw_name = Path(file.filename or "upload").name
    upload_id = f"upload-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    target_directory = harness_home_directory() / "uploads" / upload_id
    target_directory.mkdir(parents=True, exist_ok=True)
    target_path = target_directory / raw_name
    digest = hashlib.sha256()
    size = 0
    try:
        with target_path.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
                handle.write(chunk)
    finally:
        await file.close()
    mime_type = file.content_type or "application/octet-stream"
    return {
        "upload_id": upload_id,
        "title": raw_name,
        "filename": raw_name,
        "path": str(target_path),
        "mime_type": mime_type,
        "size": size,
        "sha256": digest.hexdigest(),
        "source": {
            "origin_channel": "upload",
            "source_kind": "document",
            "title": raw_name,
            "path": str(target_path),
            "metadata": {
                "upload_id": upload_id,
                "filename": raw_name,
                "mime_type": mime_type,
                "size": size,
                "sha256": digest.hexdigest(),
            },
        },
    }


# A rewriting pass-through proxy for `open_preview` of external URLs. It serves
# the page — and *every* asset and request it makes — back through this one route,
# so to the framed page everything looks same-origin (our localhost). That is what
# lets sites that refuse direct framing (`X-Frame-Options`/`frame-ancestors`) render,
# and avoids the cross-origin CORS/history errors a naive `<base>` proxy hits.

_PROXY_PATH = "/preview-proxy"
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
# different previewed sites never share them. Created lazily on the running loop.
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
# string-literal dynamic import). Served from our /preview-proxy path, relative
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
        .replace("__HARNESS_PROXY_BASE__", json.dumps(base))
        .replace("__HARNESS_PROXY_URL__", json.dumps(f"{_PROXY_PATH}?url="))
        .replace("__HARNESS_WS_PROXY_URL__", json.dumps("/preview-proxy-ws?url="))
    )
    return f"<script>\n{source}</script>"


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


@app.api_route("/preview-proxy", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def preview_proxy(url: str, request: Request):
    """Fetch an external ``http(s)`` URL server-side and re-serve it (and everything
    it links to) from our own origin, rewritten so the framed page behaves as if it
    were same-origin. This is what makes ``open_preview`` a real mini-browser for
    sites that block direct framing.

    All HTTP methods and request bodies are forwarded, a shared cookie jar keeps
    session/consent state across requests, and HTML/CSS/JS bodies are rewritten so
    links, styles, and ES-module imports keep flowing through the proxy.
    Localhost-only, like the rest of the API."""
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Only http(s) URLs can be previewed.")
    body = await request.body()
    try:
        upstream = await _get_proxy_client().request(
            request.method,
            url,
            content=body or None,
            headers=_proxy_forward_headers(request, url),
        )
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"Could not load {url}: {error}")

    final_url = str(upstream.url)
    content_type = upstream.headers.get("content-type", "application/octet-stream")
    lowered = content_type.lower()
    status = upstream.status_code
    if "html" in lowered:
        return HTMLResponse(_rewrite_proxy_html(upstream.text, final_url), status_code=status)
    if "css" in lowered:
        return Response(_rewrite_proxy_css(upstream.text, final_url), media_type=content_type, status_code=status)
    if any(token in lowered for token in ("javascript", "ecmascript", "text/jsx")):
        return Response(_rewrite_proxy_js(upstream.text, final_url), media_type=content_type, status_code=status)
    # Images, fonts, JSON, media, … — re-serve verbatim from our origin (httpx has
    # already decoded any transfer encoding), minus the headers that no longer apply.
    safe_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _PROXY_DROP_HEADERS
    }
    return Response(content=upstream.content, media_type=content_type, headers=safe_headers, status_code=status)


@app.websocket("/preview-proxy-ws")
async def preview_proxy_ws(client_socket: WebSocket):
    """Bridge a WebSocket opened by a proxied page to its real upstream server. The
    injected runtime rewrites ``new WebSocket(...)`` to point here (same-origin, so
    the browser allows it from the framed page); this relays frames both ways. Text
    and binary frames are forwarded verbatim."""
    target = client_socket.query_params.get("url", "")
    if not target.lower().startswith(("ws://", "wss://")):
        await client_socket.close(code=1008)
        return
    await client_socket.accept()
    try:
        async with websockets.connect(target, open_timeout=20) as upstream_socket:
            async def pump_client_to_upstream() -> None:
                while True:
                    message = await client_socket.receive()
                    if message.get("type") == "websocket.disconnect":
                        await upstream_socket.close()
                        return
                    if message.get("text") is not None:
                        await upstream_socket.send(message["text"])
                    elif message.get("bytes") is not None:
                        await upstream_socket.send(message["bytes"])

            async def pump_upstream_to_client() -> None:
                async for frame in upstream_socket:
                    if isinstance(frame, (bytes, bytearray)):
                        await client_socket.send_bytes(bytes(frame))
                    else:
                        await client_socket.send_text(frame)

            client_pump = asyncio.create_task(pump_client_to_upstream())
            upstream_pump = asyncio.create_task(pump_upstream_to_client())
            done, pending = await asyncio.wait(
                {client_pump, upstream_pump}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
    except (WebSocketDisconnect, OSError, websockets.WebSocketException, asyncio.TimeoutError):
        # A dropped or refused upstream WebSocket ends the bridge quietly — the page
        # sees a closed socket, exactly as it would talking to the origin directly.
        pass
    finally:
        try:
            await client_socket.close()
        except RuntimeError:
            pass


@app.get("/events")
async def events():
    """Server-sent live events (e.g. agents changed) for the UI to react to."""
    queue = _broadcaster.subscribe()

    async def event_generator():
        try:
            while True:
                event = await queue.get()
                yield {"event": "message", "data": json.dumps(event)}
        finally:
            _broadcaster.unsubscribe(queue)

    return EventSourceResponse(event_generator())


@app.post("/chat/{context_id}/permission")
async def resolve_permission(context_id: str, request: PermissionRequest):
    """Resolve a pending human-in-the-loop permission request. ``deny`` rejects;
    ``allow_once`` and ``allow_always`` both let this command run (``allow_always``
    additionally records a session rule — handled separately)."""
    future = _pending_permissions.get(request.request_id)
    if not future:
        return {"status": "unknown", "error": "No pending permission request with that identifier."}
    if future.done():
        return {"status": "stale", "error": "Permission request was already resolved."}
    # The runtime resumes on the decision string ("deny" / "allow_once" /
    # "allow_always") so it can record a session rule for "allow_always".
    future.set_result(request.decision)
    # The session is no longer waiting — refresh the sidebar marker.
    _broadcaster.publish({"type": "sessions_changed"})
    return {"status": "resolved", "decision": request.decision}


@app.post("/chat/{context_id}/question")
async def resolve_question(context_id: str, request: QuestionRequest):
    """Resolve a pending ask_user request with the user's answers."""
    future = _pending_questions.get(request.request_id)
    if not future:
        return {"status": "unknown", "error": "No pending question with that identifier."}
    if future.done():
        return {"status": "stale", "error": "Question was already resolved."}
    future.set_result(request.answers)
    _broadcaster.publish({"type": "sessions_changed"})
    return {"status": "resolved", "answers": request.answers}


@app.post("/chat/{context_id}/steer")
async def steer_context(context_id: str, request: SteeringRequest):
    """Append user steering to an active turn at the next model-call boundary."""
    message = request.message.strip()
    if not message:
        return {"queued": False}
    for executor in _executors.values():
        if executor.steer_context(context_id, message):
            return {"queued": True}
    raise HTTPException(status_code=409, detail="Session is not currently steerable.")


@app.post("/chat/{context_id}/abort")
async def abort_session(context_id: str):
    """Abort the running turn for a context and reject any pending permissions."""
    prefix = f"perm-{context_id}-"
    for request_id, future in list(_pending_permissions.items()):
        if request_id.startswith(prefix) and not future.done():
            future.set_result("deny")
    q_prefix = f"q-{context_id}-"
    for request_id, future in list(_pending_questions.items()):
        if request_id.startswith(q_prefix) and not future.done():
            # A cancelled question resolves to an empty answer list so the
            # awaiting tool call completes cleanly instead of hanging.
            future.set_result([])
    aborted = any(executor.abort_context(context_id) for executor in _executors.values())
    return {"status": "aborted" if aborted else "not_found", "session_id": context_id}


@app.post("/chat/{context_id}/tools/{tool_call_id}/abort")
async def abort_tool_call(context_id: str, tool_call_id: str):
    """Abort one foreground tool call in a running context."""
    aborted = any(executor.abort_tool(context_id, tool_call_id) for executor in _executors.values())
    return {"status": "aborted" if aborted else "not_found", "session_id": context_id, "tool_call_id": tool_call_id}


@app.post("/chat/{context_id}/permissions/mode")
async def set_permission_mode(context_id: str, request: PermissionModeRequest):
    """Set and persist the permission mode for a context's agent."""
    persisted = await asyncio.to_thread(_set_session_permission_mode, context_id, request.mode)
    if not persisted:
        raise HTTPException(status_code=404, detail="Session not found.")
    updated = any(executor.set_permission_mode(context_id, request.mode) for executor in _executors.values())
    _broadcaster.publish({"type": "sessions_changed"})
    return {"status": "updated" if updated else "saved", "mode": _normalize_permission_mode(request.mode)}


def run_server(host: str = "127.0.0.1", port: int = 8822):
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8822)
