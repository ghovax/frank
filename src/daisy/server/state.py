"""The server's shared runtime state: the singletons the boot sequence, the services, and the
route handlers read and write, plus the two small pub/sub primitives they are built on.

Extracted into one leaf module so the state is the single source of truth for "the task
store", "the registry", "the live executors", and so on — imported by ``boot``, the services,
and the routes alike. The lifespan populates these on startup (``state._task_store = ...``);
everything else reads them through this module so a rebind is seen everywhere. Type-only
imports stay under ``TYPE_CHECKING`` so this module imports nothing that would import it back.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    import httpx
    import jwt
    from sqlalchemy.orm import sessionmaker
    from a2a.server.tasks import PushNotificationSender
    from daisy.core.configuration import GlobalConfiguration
    import daisy.core.configuration as _configuration
    from daisy.core.task_store import AppendOnlyTaskStore
    from daisy.core.a2a_executor import AgentRegistry, DaisyAgentExecutor
    from daisy.core.mcp_client import MCPClientManager
    from daisy.core.remote_agents import RemoteAgentManager
    from daisy.core.a2a_files import FileUrlSigner
    from daisy.core.push_notification_store import PersistentPushNotificationConfigurationStore
    from daisy.core.file_leases import FileLeaseManager
    from daisy.core.session_workspaces import SessionWorkspaceManager
    from daisy.core.chatgpt_oauth import ChatGPTLoginFlow
    from daisy.server.services.terminals import TerminalSessionManager
    from daisy.server.services.artifacts import CaptureRequest


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


class ContextEventBus:
    """Per-context fan-out of the structured A2A parts a turn emits.

    A non-driving viewer (e.g. the sidebar re-opened on a running session) follows
    the turn by subscribing here instead of polling the task store and re-replaying
    the whole transcript every second. Normal turn parts are published after they
    are persisted. Spawned-agent progress is published immediately and journaled
    here until its queued copy reaches persistence. ``complete`` closes the stream.

    Delivery is snapshot-then-tail and gap-/duplicate-free without a cursor: a
    subscriber takes a baseline ``high_seq``, reads a compacted snapshot of the
    persisted transcript, replays journaled immediate agent events through that
    baseline, then drains its queue for events with seq > baseline and keeps
    reading live. Agent event IDs let the client deduplicate the persisted copy.
    """

    # Sentinel placed on a subscriber's queue when the context's turn completes,
    # so the SSE generator can emit a terminal frame and close cleanly.
    _DONE = object()

    def __init__(self) -> None:
        self._seq: dict[str, int] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._agent_events: dict[str, list[tuple[int, dict]]] = {}

    def publish(self, context_id: str, part: dict) -> int:
        seq = self._seq.get(context_id, 0) + 1
        self._seq[context_id] = seq
        data = part.get("data") if part.get("kind") == "data" else None
        if isinstance(data, dict) and data.get("event_id"):
            self._agent_events.setdefault(context_id, []).append((seq, part))
        for queue in self._subscribers.get(context_id, ()):
            queue.put_nowait((seq, part))
        return seq

    def complete(self, context_id: str) -> None:
        for queue in self._subscribers.get(context_id, ()):
            queue.put_nowait(self._DONE)
        self._agent_events.pop(context_id, None)

    def high_seq(self, context_id: str) -> int:
        return self._seq.get(context_id, 0)

    def agent_events_through(
        self, context_id: str, maximum_sequence: int
    ) -> list[tuple[int, dict]]:
        return [
            (sequence, part)
            for sequence, part in self._agent_events.get(context_id, ())
            if sequence <= maximum_sequence
        ]

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


# --- shared runtime singletons (populated by the boot lifespan) ---
_global_configuration: Optional[GlobalConfiguration] = None
# The host the server was told to bind to (set by run_server). Read at startup to fail
# closed when exposed on a non-loopback interface without inbound auth.
_BIND_HOST = "127.0.0.1"
_session_factory: Optional[sessionmaker] = None
_async_engine = None
_task_store: Optional[AppendOnlyTaskStore] = None
_registry: Optional[AgentRegistry] = None
_mcp_manager: Optional[MCPClientManager] = None
# Outbound A2A client manager (external agents this harness may delegate to). None until
# startup builds it from remote-agents.json; installed on the registry so make_delegate can
# branch a delegation over the wire.
_remote_agent_manager: Optional[RemoteAgentManager] = None
# Signs short-lived URLs for the A2A file-serving endpoint. Built at startup.
_file_url_signer: Optional[FileUrlSigner] = None
# Persisted push-notification configuration store and sender, shared by every mounted
# agent's handler, so a registered webhook survives a restart.
_push_configuration_store: Optional[PersistentPushNotificationConfigurationStore] = None
_push_sender: Optional[PushNotificationSender] = None
_push_httpx_client: Optional[httpx.AsyncClient] = None
_main_loop: asyncio.AbstractEventLoop | None = None
_file_lease_manager: FileLeaseManager | None = None
_workspace_manager: SessionWorkspaceManager | None = None
_terminal_manager: TerminalSessionManager | None = None
# Composio Tool Router server(s), provisioned once at startup. Kept separate from the
# mcp.json-derived servers so the file watcher's live reload re-merges them instead of
# dropping Composio whenever mcp.json changes.
_composio_servers: dict[str, _configuration.MCPServerConfiguration] = {}
_executors: dict[str, DaisyAgentExecutor] = {}
_mounted_agents: set[str] = set()
# Dialogue history per A2A context, shared across every agent executor so switching the
# active agent continues the same conversation (the persona is applied per-turn on top).
_conversations: dict[str, list] = {}
# How many executions are running per context, including delegated agents. Drives
# session-stream lifetime and the sidebar spinner; a count handles overlapping work.
_running_contexts: dict[str, int] = {}
_event_bus = ContextEventBus()
# Contexts whose latest turn is paused at input-required (durably; also populated on startup
# from persisted input-required tasks, so the marker survives a restart).
_awaiting_input_contexts: set[str] = set()
_broadcaster = Broadcaster()
# Strong references to in-flight session-title generation tasks so they are not
# garbage-collected before completing.
_title_tasks: set[Any] = set()
_capture_queue: asyncio.Queue[CaptureRequest] | None = None
# The content digest of the last configuration.yaml this process wrote, so the on-disk
# watcher can tell our own saves (skip — already applied in-process) apart from a manual user
# edit (apply). Set by every server-side configuration write.
_last_written_configuration_digest: Optional[str] = None
# Serializes every configuration mutation-and-apply (settings endpoints + the on-disk watcher)
# so they never interleave at an await point. Without it a UI save and a concurrent disk-edit
# reload could both rebuild the MCP client manager, clobbering _mcp_manager and leaking a
# half-started manager.
_configuration_lock = asyncio.Lock()
# The in-flight ChatGPT sign-in, if any. A sign-in owns a loopback server on port 1455 for its
# lifetime, so at most one runs at a time; starting a new one supersedes any stale flow. Held
# only between /auth/chatgpt/start and the browser redirect.
_chatgpt_login_flow: Optional[ChatGPTLoginFlow] = None
_jwks_clients: dict[str, jwt.PyJWKClient] = {}
_proxy_client: Optional[httpx.AsyncClient] = None
