"""What the hub layer shares: the database, the configuration, and the shared clients.

This is the half of the daemon's singletons that has nothing to do with supervising agents.
Projects, locations, settings, agent profiles, terminals and the MCP and remote-agent
connections all need a database handle and the machine's configuration; none of them needs the
session registry, the lifecycle or the prototype.

Splitting them apart is what lets the browser surface stop importing the daemon. `rest` reached
into `daemon.services` and `daemon.brokers` twenty-six times, and every one of those was a
*workspace* concern that happened to live in the daemon because that is where it was written.
Moving them out means the dependency disappears rather than being routed around.

A module of globals for the same reason the daemon's is: there is one of each per process, and
they are its parts rather than something that could sensibly exist twice. The composition root
sets them — `daemon/__main__.py` today, and `frank web` in its own process if that split is
ever taken, which is now a deployment choice rather than an architectural one.

The two hooks at the bottom are the seam's remaining edge. A workspace operation occasionally
has a supervision consequence — deleting a session should stop it — and rather than import the
control plane to say so, it calls a hook the composition root filled in. Absent, the workspace
still works; it simply has no control plane to tell.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from frank.hub.broadcast_bus import Broadcaster

# The database the workspace reads and writes, and the machine's configuration.
session_factory: Any = None
# The durable turn record. Shared rather than supervision-owned: the daemon writes it as turns
# happen, and the browser reads it to replay a transcript. Both need the same object.
turn_store: Any = None
# The registry's durable half, on the same terms and for the same reason: the daemon owns the
# registry, and the workspace services read the session rows behind it.
session_store: Any = None
async_engine: Any = None
global_configuration: Any = None
# Guards a read-modify-write of the configuration file against two clients saving at once.
configuration_lock = asyncio.Lock()
last_written_configuration_digest: Optional[str] = None

# Shared connections. One of each per process, because a stdio MCP server cannot be shared
# across processes and an A2A card fetch should not be repeated per request.
mcp_manager: Any = None
remote_agent_manager: Any = None
composio_servers: dict = {}
# The agent *profiles* a session could be created with, as A2A cards, rebuilt whenever the
# agent or skill files change. Distinct from the session registry: this is what could exist,
# that is what does.
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

# Per-session liveness the daemon learns from the event stream rather than from the registry:
# `_running_contexts` counts the turns a session currently has in flight (a session can be live
# but idle), and `_awaiting_input_contexts` marks the ones parked on a human decision. The
# registry knows whether a *process* is alive; these know what it is doing.
#
# They sit here rather than in `frank.daemon.state`, where they used to, because both layers
# need them: the daemon writes them from the event stream, and `_sessions_payload` reads them
# to say which rows are running. `daemon` may import `workspace` and not the reverse, so the
# daemon reaches these through that module's `__getattr__` and gets these very objects — while
# a copy on the daemon side would leave the session list reading an attribute that is not there.
_running_contexts: dict[str, int] = {}
_awaiting_input_contexts: set[str] = set()

# Where the daemon is listening, for the surfaces that must hand out an address.
daemon_port: int = 0
# The loop the process runs on, for the callbacks that arrive on other threads.
main_loop: Any = None

# Fan-out to every attached client: "something you are looking at changed".
broadcaster = Broadcaster()


# Where a workspace change has a supervision consequence. Filled in by the composition root;
# `None` means there is no control plane to tell, which is the correct state for a workspace
# served without one.
on_session_deleted: Optional[Callable[[str], Awaitable[Any]]] = None
reset_live_session_runtimes: Optional[Callable[[], Awaitable[Any]]] = None
refresh_live_session_locations: Optional[Callable[[str], Awaitable[Any]]] = None


async def session_deleted(session_id: str) -> None:
    """Tell the control plane a session's record has been deleted, if there is one.

    The session should stop, and only the control plane can stop it — but the workspace must
    not import the control plane to say so, or the severing would be cosmetic."""
    if on_session_deleted is None:
        return
    await on_session_deleted(session_id)


async def reset_runtimes() -> None:
    """Ask every live session to rebuild its runtime, after configuration changed."""
    if reset_live_session_runtimes is None:
        return
    await reset_live_session_runtimes()


async def workspace_locations_changed(workspace_id: str) -> None:
    """Tell the sessions running in a workspace that its environments were edited, so the
    change reaches the conversations already open in it rather than only the next one."""
    if refresh_live_session_locations is None or not workspace_id:
        return
    await refresh_live_session_locations(workspace_id)


__all__ = [
    "Broadcaster",
    "_awaiting_input_contexts",
    "_running_contexts",
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
