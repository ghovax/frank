"""What the commons layer shares: the database, the configuration, and the shared clients."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from langmesh.commons.broadcast_bus import Broadcaster

# The database the workspace reads and writes, and the machine's configuration.
session_factory: Any = None
# The durable turn record, shared because the daemon writes it and the browser reads it back.
turn_store: Any = None
# The registry's durable half, on the same terms: the daemon owns it and the services read it.
session_store: Any = None
async_engine: Any = None
global_configuration: Any = None
# Guards a read-modify-write of the configuration file against two clients saving at once.
configuration_lock = asyncio.Lock()

#: Set once the daemon has been told to stop, so a long-lived response can end itself.
shutting_down = asyncio.Event()
last_written_configuration_digest: Optional[str] = None

# Shared connections, one of each per process, since neither can usefully be repeated.
mcp_manager: Any = None
remote_agent_manager: Any = None
composio_servers: dict = {}
#: What `mcp_manager` was built from, so a write that leaves MCP alone leaves its connections alone.
mcp_server_fingerprint: Optional[str] = None
# The agent profiles a session could be created with, rebuilt whenever their files change.
agent_cards: dict = {}

# The rest of the shared machinery the browser surface reaches.
file_url_signer: Any = None
file_lease_manager: Any = None
worktree_manager: Any = None
push_configuration_store: Any = None
push_sender: Any = None
terminal_manager: Any = None
chatgpt_login_flow: Any = None
cursor_login_flow: Any = None

# Per-session liveness the daemon learns from the event stream rather than from the registry.
_running_contexts: dict[str, int] = {}
_awaiting_input_contexts: set[str] = set()
_recording_memory_contexts: dict[str, set[str]] = {}
# The goal each live session is working toward, as its worker last reported it.
_session_goals: dict[str, dict] = {}

# Where the daemon is listening, for the surfaces that must hand out an address.
daemon_port: int = 0
# The loop the process runs on, for the callbacks that arrive on other threads.
main_loop: Any = None

# Fan-out to every attached client: "something you are looking at changed".
broadcaster = Broadcaster()


# Where a workspace change has a supervision consequence, with `None` meaning there is no control plane.
on_session_deleted: Optional[Callable[[str], Awaitable[Any]]] = None
reset_live_session_runtimes: Optional[Callable[[], Awaitable[Any]]] = None
refresh_live_session_locations: Optional[Callable[[str], Awaitable[Any]]] = None


async def session_deleted(session_id: str) -> None:
    """Tell the control plane a session's record has been deleted, if there is one."""
    _recording_memory_contexts.pop(session_id, None)
    if on_session_deleted is None:
        return
    await on_session_deleted(session_id)


async def reset_runtimes() -> None:
    """Ask every live session to rebuild its runtime, after configuration changed."""
    if reset_live_session_runtimes is None:
        return
    await reset_live_session_runtimes()


async def workspace_locations_changed(workspace_id: str) -> None:
    """Tell the sessions in a workspace that its locations were edited, so open conversations see it."""
    if refresh_live_session_locations is None or not workspace_id:
        return
    await refresh_live_session_locations(workspace_id)


__all__ = [
    "Broadcaster",
    "_awaiting_input_contexts",
    "_recording_memory_contexts",
    "_running_contexts",
    "_session_goals",
    "agent_cards",
    "async_engine",
    "broadcaster",
    "chatgpt_login_flow",
    "cursor_login_flow",
    "composio_servers",
    "configuration_lock",
    "daemon_port",
    "file_lease_manager",
    "file_url_signer",
    "global_configuration",
    "last_written_configuration_digest",
    "main_loop",
    "mcp_manager",
    "on_session_deleted",
    "push_configuration_store",
    "push_sender",
    "remote_agent_manager",
    "refresh_live_session_locations",
    "reset_live_session_runtimes",
    "reset_runtimes",
    "session_deleted",
    "session_factory",
    "turn_store",
    "terminal_manager",
    "workspace_locations_changed",
    "worktree_manager",
]
