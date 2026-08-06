"""An MCP server that echoes what it is given, the smallest one that works."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool()
def echo(text: str) -> str:
    """Return the same text back unchanged, useful only for confirming the harness can reach this server."""
    return text


if __name__ == "__main__":
    mcp.run(transport="stdio")
