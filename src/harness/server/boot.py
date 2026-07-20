"""The server's composition root: it builds the FastAPI ``app``, wires the auth
middleware and CORS handler, mounts each configured agent's A2A sub-app, drives the
startup/shutdown lifespan, and runs the config/agent/host watchers.

The domain logic it orchestrates lives in :mod:`harness.server.services` (per concern),
the shared singletons in :mod:`harness.server.state`, the ORM in
:mod:`harness.server.database`, and the DTOs in :mod:`harness.server.models` — this module
imports those, never a route module, so there is no ``app -> routes -> app`` cycle. The
thin :mod:`harness.server.app` sits on top: it takes this ``app``, mounts the REST route
routers onto it, and exposes ``run_server``.
"""

import asyncio
import hmac
import ipaddress
import jwt
import logging
import re

import httpx
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from harness.server import state
from harness.server.services.remote_agents import (
    _poll_remote_agent_health,
    _reload_remote_agents,
    _remote_agent_dataclasses,
)
from harness.server.services.projects import (
    _ensure_default_project,
)
from harness.server.services.settings import (
    _configuration_digest,
    _rebuild_web_fetch_clients,
    _reload_configuration_from_disk,
)
from harness.server.services.mcp import (
    _ensure_mcp_servers_for,
    _reload_mcp,
)
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
    _session_permission_mode_for,
)
from harness.server.services.artifacts import (
    _capture_artifacts,
    _capture_worker,
)
from harness.server.services.locations import (
    _resolve_session_locations,
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
    _apply_history_schema,
)
from watchfiles import awatch
from dotenv import load_dotenv

from a2a.server.apps.jsonrpc import A2AFastAPIApplication
from a2a.server.request_handlers import DefaultRequestHandler

from harness.core.a2a_executor import (
    AgentRegistry,
    HarnessAgentExecutor,
    agent_rpc_path,
)
from harness.core.remote_agents import RemoteAgentManager
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
    seed_home_agents,
)
from harness.core.composio_router import composio_mcp_servers
from harness.core.mcp_client import MCPClientManager
from harness.core.background import reap_orphaned_processes
from harness.core.file_leases import FileLeaseManager
from harness.core.session_workspaces import SessionWorkspaceManager
from harness.core.sqlite_lock import configure_sqlite_lock, sqlite_write_lock
from harness.tools.tools import (
    cancel_all_background_tasks,
    set_exa_client,
    set_mcp_client_manager,
)
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




def _configure_telemetry(configuration: GlobalConfiguration) -> None:
    telemetry_configuration = configuration.telemetry
    _telemetry.configure(
        enabled=telemetry_configuration.enabled,
        endpoint=telemetry_configuration.exporter.endpoint,
        headers=telemetry_configuration.resolved_headers(),
        sample_ratio=telemetry_configuration.sample_ratio,
    )










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
