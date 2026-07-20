"""Settings service: helpers split out of the server runtime."""

from harness.core.composio_router import composio_mcp_servers
from harness.core.configuration import GlobalConfiguration
from harness.core.configuration import configuration_file_path
from harness.core.configuration import save_api_keys
from harness.core.mcp_client import MCPClientManager
from harness.core.tuning import set_tuning
from harness.core.tuning import tuning_from_policy
from harness.tools.file_tools import set_firecrawl_client
from harness.tools.file_tools import set_jina_api_key
from harness.tools.file_tools import set_proxy_url
from harness.tools.tools import set_exa_client
from harness.tools.tools import set_mcp_client_manager
from typing import Optional
import asyncio
import hashlib
from harness.server import state
from harness.server.services.sessions import _reset_work_habits_acknowledgements


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
