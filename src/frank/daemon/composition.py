"""What the daemon owns on behalf of everyone, and the watchers that keep it current."""

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

    # Seed the home layer with editable copies of the shipped agents and skills, non-destructively.
    seeded = await asyncio.to_thread(seed_home_agents)
    if seeded:
        logger.info("seeded home agents and skills: %s", ", ".join(seeded))
    # Seed the digest with the file as just loaded, so a bootstrap write is not mistaken for a manual edit.
    hub_state.last_written_configuration_digest = await asyncio.to_thread(_configuration_digest)

    # There is no landing page, so the app always opens into a project: guarantee one exists.
    await asyncio.to_thread(_ensure_default_project)

    # Toolboxes outlive a daemon that was killed, so ones belonging to gone sessions are swept.
    live_sessions = [record.id for record in state.registry.live()] if state.registry is not None else []
    swept = await asyncio.to_thread(toolbox.sweep, live_sessions)
    if swept:
        logger.info("swept %d toolbox(es) belonging to sessions that are gone", len(swept))

    # Composio's hosted endpoint is folded into the ordinary server set rather than being a second path.
    hub_state.composio_servers = composio_mcp_servers(configuration.composio)
    configuration.mcp.servers.update(hub_state.composio_servers)
    mcp_servers = configuration.mcp.enabled_servers()
    hub_state.mcp_manager = MCPClientManager(mcp_servers) if mcp_servers else None
    if hub_state.mcp_manager is not None:
        # Connected in the background, so a slow or hung server never delays the daemon's boot.
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

    # Outbound peers, whose card resolution runs in the background so an unreachable one never holds up boot.
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
        # Recurring prompts, alongside the watchers because they are the same kind of long-lived task.
        asyncio.create_task(scheduler.run()),
    ]


async def close_shared_resources() -> None:
    """Release everything the opener built, ordered and individually guarded."""
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
    """Every directory whose contents define what agents and skills exist, watched recursively."""
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
    """Mirror hand edits of the configuration file into the running daemon and its clients."""
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
            # Serialised against interface-driven saves, with the digest re-checked inside the lock.
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
    """Broadcast when the SSH host registry changes, filtered to the configuration file alone."""
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
