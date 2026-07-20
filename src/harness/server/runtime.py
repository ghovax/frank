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
import hashlib
import hmac
import ipaddress
import jwt
import uuid
import logging
import re
import subprocess

import httpx
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from harness.server import state
from harness.server.services.agents import (
    AGENT_CARD_PATH,
    PUBLIC_BASE_URL,
    _agent_configuration_for_request,
    _card_for,
    _load_agent_sidecar,
    _reload_agent_cards,
    _save_agent_sidecar,
)
from harness.server.services.sessions import (
    _claim_work_habits_acknowledgement,
    _ensure_session_workspace,
    _record_session_visible,
    _reset_work_habits_acknowledgements,
    _session_permission_mode_for,
)
from harness.server.services.artifacts import (
    _capture_artifacts,
    _capture_worker,
)
from harness.server.services.locations import (
    _add_location_row,
    _derive_location_name,
    _existing_location_entries,
    _iso_now,
    _locations_conflict_message,
    _resolve_session_locations,
    _serialize_location,
    _serialize_project,
)
from harness.server.services.broadcast import (
    _notify_filesystem_lease_state,
    _notify_permission_state,
    _publish_broadcast,
    _publish_stream_event,
    _set_turn_state,
)
from harness.server.services.terminals import (
    TerminalSessionManager,
)
from harness.server.database import (
    SessionRecord,
    ProjectRecord,
    LocationRecord,
    _apply_history_schema,
)
from watchfiles import awatch
from dotenv import load_dotenv
from harness.server.models import (
    LocationInput,
    ProjectCreateRequest,
)

from a2a.server.apps.jsonrpc import A2AFastAPIApplication
from a2a.server.request_handlers import DefaultRequestHandler

from harness.core.a2a_executor import (
    AgentRegistry,
    HarnessAgentExecutor,
    agent_rpc_path,
)
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
    configuration_file_path,
    database_file_path,
    harness_home_directory,
    list_agent_route_names,
    save_api_keys,
    seed_home_agents,
)
from harness.core.composio_router import composio_mcp_servers
from harness.core.mcp_client import MCPClientManager
from harness.core.background import reap_orphaned_processes
from harness.core.file_leases import FileLeaseManager
from harness.core.session_workspaces import SessionWorkspaceManager
from harness.core.sqlite_lock import configure_sqlite_lock, sqlite_write_lock
from harness.locations import ssh_hosts as _ssh_hosts
from harness.tools.tools import (
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












# Contexts whose latest turn is paused at input-required (durably; also populated on
# startup from persisted input-required tasks, so the marker survives a restart).



# Keeps references to in-flight session-title generation tasks so they are not
# garbage-collected before completing.
































def _project_name(path: str) -> str:
    normalized = path.rstrip("/\\")
    return Path(normalized).name or normalized or path








# Artifact versioning — shadow-git capture of the specific files the agent touches (never a
# folder survey). Capture is silent and best-effort: a write-ish tool call enqueues a request
# and returns immediately; a single background worker drains the queue and runs the git
# plumbing (``core/artifact_versioning``) off-loop against the write's location — local or
# remote — then records the DB index rows and broadcasts so open panels refresh. Failures are
# logged, never fatal (a versioning hiccup must not break the agent's turn).




















































# Loads the title prompt from the shared prompts directory next to the
# harness.core package, mirroring how AgentRuntime resolves its prompt loader.












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






# A rewriting pass-through proxy for `open_artifact` of external URLs. It serves
# the page — and *every* asset and request it makes — back through this one route,
# so to the framed page everything looks same-origin (our localhost). That is what
# lets sites that refuse direct framing (`X-Frame-Options`/`frame-ancestors`) render,
# and avoids the cross-origin CORS/history errors a naive `<base>` proxy hits.

# URL schemes that must never be rewritten through the proxy.
# Response headers dropped when re-serving (framing blockers + hop-by-hop/encoding
# headers that no longer match the rewritten body). set-cookie is dropped from the
# BROWSER response (its cookies would be scoped to our localhost origin, useless)
# but the cookies are still stored server-side by the shared cookie-jar client and
# replayed upstream, so login/consent/session flows survive across proxied requests.
# Request headers never forwarded upstream — hop-by-hop, or ones httpx/the target
# must recompute for the real origin rather than inherit from our localhost frame.

# One long-lived client so the upstream cookie jar (session, consent, CSRF cookies)
# persists across every proxied request. Cookies are domain-scoped by httpx, so
# different opened sites never share them. Created lazily on the running loop.



# Tags/attributes that would fight the proxy: an inline CSP, a <base> that would
# re-point relative URLs, and SRI/crossorigin hints that fail once same-origin.






# ES-module specifiers the browser resolves itself (static import/export-from and
# string-literal dynamic import). Served from our /artifact-proxy path, relative
# specifiers would otherwise resolve against localhost and 404 — so every literal
# specifier is rewritten to an absolute, proxied URL. Computed specifiers in a
# dynamic import() cannot be rewritten statically (the runtime shim cannot patch the
# import operator either); those remain a known gap.
























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
