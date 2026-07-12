"""Minimal example MCP server — a template to copy and customize.

This exposes one trivial tool (`echo`) over the Model Context Protocol so the
harness agent can call it. It exists so you can see exactly how a custom MCP
server is structured and wire one of your own.

To add your own tool:
  1. Copy this file (for example to `examples/mcp/my_server/server.py`).
  2. Add one or more functions decorated with `@mcp.tool()` — each becomes a tool
     the agent can call. Type hints + the docstring become the tool's schema and
     description.
  3. Register it in `.agents/mcp.json` under `mcpServers` (see the `example`
     entry there, which points back at this file and is disabled by default).
  4. Flip `"enabled": true` on your entry to turn it on.

Run it standalone to try it out:
    uv run python examples/mcp/example/server.py
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("example")


@mcp.tool()
def echo(text: str) -> str:
    """Return the same text back unchanged.

    A no-op tool, useful only for confirming the harness can reach this server.
    Replace it with something real.
    """
    return text


if __name__ == "__main__":
    mcp.run(transport="stdio")
