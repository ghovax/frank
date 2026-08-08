"""Expose Composio's hosted MCP endpoint as an ordinary MCP server."""

from __future__ import annotations

import logging

from langmesh.base.configuration import ComposioConfiguration, MCPServerConfiguration

logger = logging.getLogger(__name__)


def composio_mcp_servers(
    configuration: ComposioConfiguration,
) -> dict[str, MCPServerConfiguration]:
    """Composio's endpoint as one server entry, or an empty dict when it is disabled or unconfigured."""
    if not configuration.enabled:
        return {}

    if not configuration.url:
        logger.warning("Composio is enabled but no MCP URL is set, skipping its tools")
        return {}

    api_key = configuration.effective_api_key
    if not api_key:
        logger.warning(
            "Composio is enabled but no API key is set (composio.api_key or COMPOSIO_API_KEY); skipping Composio tools."
        )
        return {}

    logger.info(
        "Composio hosted MCP configured as server '%s' (%s).",
        configuration.server_name,
        configuration.url,
    )
    return {
        configuration.server_name: MCPServerConfiguration(
            enabled=True,
            transport="streamable_http",
            stateful=True,
            url=configuration.url,
            headers={"x-consumer-api-key": api_key},
            timeout_seconds=configuration.timeout_seconds,
        )
    }
