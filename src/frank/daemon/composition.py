"""What the daemon owns on behalf of everyone, and the watchers that keep it current.

The registry, the prototype and the stores are the daemon's *lifecycle* half, built in
:mod:`frank.daemon.__main__`. This is the other half: what has to be singular — file leases
that coordinate writes between sessions, workspaces, terminals, signed file URLs, push
notifications, remote peers — plus the GUI surface that reads and edits them.

They live here rather than in a worker because there can only sensibly be one of each. A
file lease is only a lease if a single process arbitrates it.

The MCP pool is the exception that proves the rule. The daemon keeps one for the GUI's
server browser, but a session connects its own for its tool calls: MCP connections are
stateful and a stdio server is a subprocess, neither of which crosses a process boundary.
That is a real cost of process-per-session, taken deliberately.

Deliberately free of `frank.runtime` at import: the three GUI endpoints that genuinely need
the runtime import it when called. Keeping the daemon's startup graph clear of LangChain
and LiteLLM is what keeps it small enough to be the always-on process.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from frank.base.configuration import (
    configuration_file_path,
    list_agent_route_names,
    seed_home_agents,
)
from frank.base import toolbox
from frank.base.file_leases import FileLeaseManager
from frank.base.paths import data_directory
from frank.base.worktrees import SessionWorktreeManager
from frank.daemon import state
from frank.hub import state as hub_state
from frank.hub.services.agents import _reload_agent_cards
from frank.hub.services.broadcast import _notify_filesystem_lease_state
from frank.hub.services.workspaces import _ensure_default_project
from frank.hub.services.settings import _configuration_digest, _reload_configuration_from_disk

logger = logging.getLogger(__name__)

async def open_shared_resources() -> None:
    """Build what the daemon holds for everyone, in dependency order."""
    from frank.hub.brokers.composio import composio_mcp_servers
    from frank.base.mcp_client import MCPClientManager
    from frank.hub.brokers.remote_agents import _remote_agent_dataclasses
    from frank.daemon.persistence.push_store import (
        PersistentPushNotificationConfigurationStore,
        PinnedPushNotificationSender,
    )
    from frank.protocol.files import FileUrlSigner, load_or_create_secret
    from frank.hub.brokers.terminals import TerminalSessionManager

    assert hub_state.global_configuration is not None
    configuration = hub_state.global_configuration

    hub_state.main_loop = asyncio.get_running_loop()
    hub_state.file_lease_manager = FileLeaseManager(on_change=_notify_filesystem_lease_state)
    hub_state.worktree_manager = SessionWorktreeManager()
    hub_state.terminal_manager = TerminalSessionManager()

    # Seed the home layer (~/.agents) with editable copies of the shipped agents and skills, non-destructively.
    seeded = await asyncio.to_thread(seed_home_agents)
    if seeded:
        logger.info("seeded home agents and skills: %s", ", ".join(seeded))
    # Seed the digest with the file as just loaded, so the bootstrap write that `Configuration.load` may have performed is not mistaken for a manual edit by the watcher below and echoed straight back.
    hub_state.last_written_configuration_digest = await asyncio.to_thread(_configuration_digest)

    # There is no landing page, so the app always opens into a project: guarantee one exists.
    await asyncio.to_thread(_ensure_default_project)

    # The tools sessions installed for themselves outlive a daemon that was killed rather than asked to stop, and a directory belonging to a session that no longer exists is only taking up space.
    live_sessions = [record.id for record in state.registry.live()] if state.registry is not None else []
    swept = await asyncio.to_thread(toolbox.sweep, live_sessions)
    if swept:
        logger.info("swept %d toolbox(es) belonging to sessions that are gone", len(swept))

    # Composio's hosted endpoint is folded into the ordinary MCP set rather than being a second path, so tool gating and the client manager both see it as just another server.
    hub_state.composio_servers = composio_mcp_servers(configuration.composio)
    configuration.mcp.servers.update(hub_state.composio_servers)
    mcp_servers = configuration.mcp.enabled_servers()
    hub_state.mcp_manager = MCPClientManager(mcp_servers) if mcp_servers else None
    if hub_state.mcp_manager is not None:
        # Connected in the background: a slow or hung server — a cold `uvx` spawn, a stalled endpoint — must never delay the daemon's boot.
        state._mcp_start_task = asyncio.create_task(hub_state.mcp_manager.start())

    signing_root = data_directory()
    hub_state.file_url_signer = FileUrlSigner(
        load_or_create_secret(signing_root),
        f"http://127.0.0.1:{hub_state.daemon_port}",
        allowed_root=signing_root / "uploads",
    )

    hub_state.push_configuration_store = PersistentPushNotificationConfigurationStore(hub_state.async_engine)
    await hub_state.push_configuration_store.initialize()
    import httpx

    state._push_client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    hub_state.push_sender = PinnedPushNotificationSender(
        state._push_client,
        hub_state.push_configuration_store,
        allow_private=hub_state.push_configuration_store.allow_private_webhooks,
    )

    # Outbound A2A to peers declared in remote-agents.json.
    remote_configurations = _remote_agent_dataclasses()
    if remote_configurations:
        from frank.protocol.client import RemoteAgentManager

        hub_state.remote_agent_manager = RemoteAgentManager(remote_configurations)
        state._remote_start_task = asyncio.create_task(hub_state.remote_agent_manager.start())

    _reload_agent_cards()
    from frank.daemon import scheduler

    state._watchers = [
        asyncio.create_task(_watch_agents_and_skills()),
        asyncio.create_task(_watch_configuration()),
        asyncio.create_task(_watch_ssh_hosts()),
        # Recurring prompts.
        asyncio.create_task(scheduler.run()),
    ]


async def close_shared_resources() -> None:
    """Release everything `open_shared_resources` built. Ordered so nothing is torn down
    while something else is still using it, and individually guarded so one failure cannot
    strand the rest."""
    for task in [*getattr(state, "_watchers", []), state.__dict__.get("_mcp_start_task"), state.__dict__.get("_remote_start_task")]:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    if hub_state.terminal_manager is not None:
        with contextlib.suppress(Exception):
            await hub_state.terminal_manager.close_all()
    if hub_state.mcp_manager is not None:
        with contextlib.suppress(Exception):
            await hub_state.mcp_manager.aclose()
    for client in (state.__dict__.get("_push_client"), state.proxy_client):
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()


def _watched_agent_paths() -> list[str]:
    """Every directory whose contents define what agents and skills exist.

    The `.agents` roots are watched recursively so `mcp.json` and `remote-agents.json` are
    picked up alongside the profiles themselves — all three are live, and the only thing that
    needs a restart is a change to the harness itself."""
    assert hub_state.global_configuration is not None
    candidates = [
        *hub_state.global_configuration.agents_root_directories(),
        *hub_state.global_configuration.agent_directories(),
        *hub_state.global_configuration.skill_directories(),
    ]
    watched: list[str] = []
    seen: set[Path] = set()
    for directory in candidates:
        if not directory.is_dir():
            continue
        resolved = directory.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        watched.append(str(resolved))
    return watched


async def _watch_agents_and_skills() -> None:
    """Pick up agents, skills, MCP servers, and remote peers as they change on disk."""
    from watchfiles import awatch

    from frank.hub.brokers.mcp_servers import _reload_mcp
    from frank.hub.brokers.remote_agents import _reload_remote_agents

    watched = _watched_agent_paths()
    if not watched:
        return
    try:
        async for changes in awatch(*watched, stop_event=hub_state.shutting_down):
            paths = [str(path) for _change, path in changes]
            if any(path.endswith("mcp.json") for path in paths):
                await _reload_mcp()
            if any(path.endswith("remote-agents.json") for path in paths):
                await _reload_remote_agents()
            _reload_agent_cards()
            state.broadcaster.publish({"type": "agents_changed"})
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 — a watcher that dies must not take the daemon with it
        logger.exception("the agent and skill watcher stopped")


async def _watch_configuration() -> None:
    """Mirror hand edits of the configuration file into the running daemon and its clients.

    The file is the single source of truth, so editing an API key in it takes effect without
    a restart. Our own writes come back as changes too; they are recognised by digest and
    skipped, so a save made in the UI does not echo round as an external edit."""
    from watchfiles import awatch

    from frank.hub.services.settings import _configuration_digest as digest_of

    path = configuration_file_path()
    try:
        async for _changes in awatch(
            str(path.parent),
            recursive=False,
            watch_filter=lambda _change, changed: Path(changed).name == path.name,
            stop_event=hub_state.shutting_down,
        ):
            # Serialised against UI-driven saves, and the digest is re-checked *inside* the lock so a save that landed while we waited is recognised as ours.
            async with hub_state.configuration_lock:
                digest = await asyncio.to_thread(digest_of)
                if digest is not None and digest == hub_state.last_written_configuration_digest:
                    continue
                hub_state.last_written_configuration_digest = digest
                await _reload_configuration_from_disk()
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("the configuration watcher stopped")


async def _watch_ssh_hosts() -> None:
    """Broadcast when the SSH host registry changes, so host pickers refresh in place.

    Filtered to `config` alone: the same directory holds keys and `known_hosts`, which churn
    for reasons that have nothing to do with which hosts are defined."""
    from watchfiles import awatch

    ssh_config = Path("~/.ssh/config").expanduser()
    if not ssh_config.parent.exists():
        return
    try:
        async for _changes in awatch(
            str(ssh_config.parent),
            recursive=False,
            watch_filter=lambda _change, changed: Path(changed).name == "config",
            stop_event=hub_state.shutting_down,
        ):
            state.broadcaster.publish({"type": "hosts_changed"})
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("the SSH host watcher stopped")


def known_agent_names() -> list[str]:
    """Every agent profile a session could be created with, from the configured roots."""
    assert hub_state.global_configuration is not None
    return list_agent_route_names(hub_state.global_configuration.agent_directories())


__all__ = [
    "close_shared_resources",
    "known_agent_names",
    "open_shared_resources",
]
