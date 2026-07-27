"""What the daemon owns on behalf of everyone, and the watchers that keep it current.

The registry, the prototype and the stores are the daemon's *lifecycle* half, built in
:mod:`daisy.daemon.__main__`. This is the other half: what has to be singular — file leases
that coordinate writes between sessions, workspaces, terminals, signed file URLs, push
notifications, remote peers — plus the GUI surface that reads and edits them.

They live here rather than in a worker because there can only sensibly be one of each. A
file lease is only a lease if a single process arbitrates it.

The MCP pool is the exception that proves the rule. The daemon keeps one for the GUI's
server browser, but a session connects its own for its tool calls: MCP connections are
stateful and a stdio server is a subprocess, neither of which crosses a process boundary.
That is a real cost of process-per-session, taken deliberately.

Deliberately free of `daisy.runtime` at import: the three GUI endpoints that genuinely need
the runtime import it when called. Keeping the daemon's startup graph clear of LangChain
and LiteLLM is what keeps it small enough to be the always-on process.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from daisy.base.configuration import (
    configuration_file_path,
    list_agent_route_names,
    seed_home_agents,
)
from daisy.base.file_leases import FileLeaseManager
from daisy.base.paths import data_directory
from daisy.base.workspaces import SessionWorkspaceManager
from daisy.daemon import state
from daisy.workspace import state as workspace_state
from daisy.workspace.services.agents import _reload_agent_cards
from daisy.workspace.services.broadcast import _notify_filesystem_lease_state
from daisy.workspace.services.projects import _ensure_default_project
from daisy.workspace.services.settings import _configuration_digest, _reload_configuration_from_disk

logger = logging.getLogger(__name__)

async def open_shared_resources() -> None:
    """Build what the daemon holds for everyone, in dependency order."""
    from daisy.workspace.brokers.composio import composio_mcp_servers
    from daisy.base.mcp_client import MCPClientManager
    from daisy.workspace.brokers.remote_agents import _remote_agent_dataclasses
    from daisy.daemon.persistence.push_store import (
        PersistentPushNotificationConfigurationStore,
        PinnedPushNotificationSender,
    )
    from daisy.protocol.files import FileUrlSigner, load_or_create_secret
    from daisy.workspace.brokers.terminals import TerminalSessionManager

    assert workspace_state.global_configuration is not None
    configuration = workspace_state.global_configuration

    workspace_state.main_loop = asyncio.get_running_loop()
    workspace_state.file_lease_manager = FileLeaseManager(on_change=_notify_filesystem_lease_state)
    workspace_state.workspace_manager = SessionWorkspaceManager()
    workspace_state.terminal_manager = TerminalSessionManager()

    # Seed the home layer (~/.agents) with editable copies of the shipped agents and skills,
    # non-destructively. This is what makes the bundled profiles appear in a packaged build
    # and gives each a writable copy, since the bundled originals are read-only inside the
    # frozen application.
    seeded = await asyncio.to_thread(seed_home_agents)
    if seeded:
        logger.info("Seeded home agents and skills: %s", ", ".join(seeded))
    # Seed the digest with the file as just loaded, so the bootstrap write that
    # `GlobalConfiguration.load` may have performed is not mistaken for a manual edit by the
    # watcher below and echoed straight back.
    workspace_state.last_written_configuration_digest = await asyncio.to_thread(_configuration_digest)

    # There is no landing page, so the app always opens into a project: guarantee one exists.
    await asyncio.to_thread(_ensure_default_project)

    # Composio's hosted endpoint is folded into the ordinary MCP set rather than being a
    # second path, so tool gating and the client manager both see it as just another server.
    workspace_state.composio_servers = composio_mcp_servers(configuration.composio)
    configuration.mcp.servers.update(workspace_state.composio_servers)
    mcp_servers = configuration.mcp.enabled_servers()
    workspace_state.mcp_manager = MCPClientManager(mcp_servers) if mcp_servers else None
    if workspace_state.mcp_manager is not None:
        # Connected in the background: a slow or hung server — a cold `uvx` spawn, a stalled
        # endpoint — must never delay the daemon's boot. Tool gating keys on the manager
        # existing, not on live connections, so each server's tools appear as it finishes.
        state._mcp_start_task = asyncio.create_task(workspace_state.mcp_manager.start())

    signing_root = data_directory()
    workspace_state.file_url_signer = FileUrlSigner(
        load_or_create_secret(signing_root),
        f"http://127.0.0.1:{workspace_state.daemon_port}",
        allowed_root=signing_root / "uploads",
    )

    workspace_state.push_configuration_store = PersistentPushNotificationConfigurationStore(workspace_state.async_engine)
    await workspace_state.push_configuration_store.initialize()
    import httpx

    state._push_client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    workspace_state.push_sender = PinnedPushNotificationSender(
        state._push_client,
        workspace_state.push_configuration_store,
        allow_private=workspace_state.push_configuration_store.allow_private_webhooks,
    )

    # Outbound A2A to peers declared in remote-agents.json. Card resolution is best effort
    # and started in the background, so an unreachable peer never holds up boot.
    remote_configurations = _remote_agent_dataclasses()
    if remote_configurations:
        from daisy.protocol.client import RemoteAgentManager

        workspace_state.remote_agent_manager = RemoteAgentManager(remote_configurations)
        state._remote_start_task = asyncio.create_task(workspace_state.remote_agent_manager.start())

    _reload_agent_cards()
    state._watchers = [
        asyncio.create_task(_watch_agents_and_skills()),
        asyncio.create_task(_watch_configuration()),
        asyncio.create_task(_watch_ssh_hosts()),
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
    if workspace_state.terminal_manager is not None:
        with contextlib.suppress(Exception):
            await workspace_state.terminal_manager.close_all()
    if workspace_state.mcp_manager is not None:
        with contextlib.suppress(Exception):
            await workspace_state.mcp_manager.aclose()
    for client in (state.__dict__.get("_push_client"), state.proxy_client):
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()


def _watched_agent_paths() -> list[str]:
    """Every directory whose contents define what agents and skills exist.

    The `.agents` roots are watched recursively so `mcp.json` and `remote-agents.json` are
    picked up alongside the profiles themselves — all three are live, and the only thing that
    needs a restart is a change to the harness itself."""
    assert workspace_state.global_configuration is not None
    candidates = [
        *workspace_state.global_configuration.agents_root_directories(),
        *workspace_state.global_configuration.agent_directories(),
        *workspace_state.global_configuration.skill_directories(),
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

    from daisy.workspace.brokers.mcp_servers import _reload_mcp
    from daisy.workspace.brokers.remote_agents import _reload_remote_agents

    watched = _watched_agent_paths()
    if not watched:
        return
    try:
        async for changes in awatch(*watched):
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
        logger.exception("The agent and skill watcher stopped")


async def _watch_configuration() -> None:
    """Mirror hand edits of the configuration file into the running daemon and its clients.

    The file is the single source of truth, so editing an API key in it takes effect without
    a restart. Our own writes come back as changes too; they are recognised by digest and
    skipped, so a save made in the UI does not echo round as an external edit."""
    from watchfiles import awatch

    from daisy.workspace.services.settings import _configuration_digest as digest_of

    path = configuration_file_path()
    try:
        async for _changes in awatch(
            str(path.parent),
            recursive=False,
            watch_filter=lambda _change, changed: Path(changed).name == path.name,
        ):
            # Serialised against UI-driven saves, and the digest is re-checked *inside* the
            # lock so a save that landed while we waited is recognised as ours.
            async with workspace_state.configuration_lock:
                digest = await asyncio.to_thread(digest_of)
                if digest is not None and digest == workspace_state.last_written_configuration_digest:
                    continue
                workspace_state.last_written_configuration_digest = digest
                await _reload_configuration_from_disk()
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("The configuration watcher stopped")


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
        ):
            state.broadcaster.publish({"type": "hosts_changed"})
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("The SSH host watcher stopped")


def known_agent_names() -> list[str]:
    """Every agent profile a session could be created with, from the configured roots."""
    assert workspace_state.global_configuration is not None
    return list_agent_route_names(workspace_state.global_configuration.agent_directories())


__all__ = [
    "close_shared_resources",
    "known_agent_names",
    "open_shared_resources",
]
