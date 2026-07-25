"""Expose Composio's hosted MCP endpoint as an MCP server.

Composio's dashboard hands you a ready "connect" MCP URL plus an API key.
That endpoint is the Tool Router: a small set of meta-tools (search, schema fetch,
execute, manage-connections) over `streamable_http`, through which the agent
discovers and runs tools across whichever toolkits the dashboard server enables.

So integration is just config: build a `streamable_http` MCP server pointed at the
URL with the API key as the `x-consumer-api-key` header. The actual
connection is made by the MCP client manager at startup like any other server;
nothing here does I/O, so a bad URL/key surfaces as a normal MCP connection error
rather than crashing boot.
"""

from __future__ import annotations

import logging

from xeac.base.configuration import ComposioConfiguration, MCPServerConfiguration

logger = logging.getLogger(__name__)


def composio_mcp_servers(
    configuration: ComposioConfiguration,
) -> dict[str, MCPServerConfiguration]:
    """Return Composio's hosted MCP endpoint as a single MCP server entry keyed by
    ``configuration.server_name`` (empty dict if disabled or not configured)."""
    if not configuration.enabled:
        return {}

    if not configuration.url:
        logger.warning("Composio is enabled but no MCP url is set; skipping Composio tools.")
        return {}

    api_key = configuration.effective_api_key
    if not api_key:
        logger.warning(
            "Composio is enabled but no API key is set "
            "(composio.api_key or COMPOSIO_API_KEY); skipping Composio tools."
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
