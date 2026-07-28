"""An MCP server that echoes what it is given — the smallest one that works.

It exposes a single tool over the Model Context Protocol so you can see how a server is
structured, wire it up, and watch the agent call it. Named for what it does rather than for
what it is: everything under `examples/` is an example, so calling this one "example" said
nothing twice.

To add your own tool:
  1. Copy this file, to `examples/mcp/<your-server>/server.py`.
  2. Add one or more functions decorated with `@mcp.tool()` — each becomes a tool
     the agent can call. Type hints + the docstring become the tool's schema and
     description.
  3. Register it in `.agents/mcp.json` under `mcpServers` (see the `echo`
     entry there, which points back at this file and is disabled by default).
  4. Flip `"enabled": true` on your entry to turn it on.

Run it standalone to try it out:
    uv run python examples/mcp/echo/server.py
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool()
def echo(text: str) -> str:
    """Return the same text back unchanged.

    A no-op tool, useful only for confirming the harness can reach this server.
    Replace it with something real.
    """
    return text


if __name__ == "__main__":
    mcp.run(transport="stdio")
