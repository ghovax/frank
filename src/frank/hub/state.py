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
# The durable turn record.
turn_store: Any = None
# The registry's durable half, on the same terms and for the same reason: the daemon owns the registry, and the workspace services read the session rows behind it.
session_store: Any = None
async_engine: Any = None
global_configuration: Any = None
# Guards a read-modify-write of the configuration file against two clients saving at once.
configuration_lock = asyncio.Lock()

#: Set once the daemon has been told to stop, before its listeners are asked to drain.
shutting_down = asyncio.Event()
last_written_configuration_digest: Optional[str] = None

# Shared connections.
mcp_manager: Any = None
remote_agent_manager: Any = None
composio_servers: dict = {}
# The agent *profiles* a session could be created with, as A2A cards, rebuilt whenever the agent or skill files change.
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

# Per-session liveness the daemon learns from the event stream rather than from the registry: `_running_contexts` counts the turns a session currently has in flight (a session can be live but idle), and `_awaiting_input_contexts` marks the ones parked on a human decision.
_running_contexts: dict[str, int] = {}
_awaiting_input_contexts: set[str] = set()
# The goal each live session is working toward, as its worker last reported it.
_session_goals: dict[str, dict] = {}

# Where the daemon is listening, for the surfaces that must hand out an address.
daemon_port: int = 0
# The loop the process runs on, for the callbacks that arrive on other threads.
main_loop: Any = None

# Fan-out to every attached client: "something you are looking at changed".
broadcaster = Broadcaster()


# Where a workspace change has a supervision consequence.
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
