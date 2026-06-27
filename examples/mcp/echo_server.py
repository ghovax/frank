from mcp.server.fastmcp import FastMCP


mcp = FastMCP("harness-demo")


@mcp.tool()
def echo(text: str) -> str:
    """Echo text back to the caller."""
    return text


@mcp.tool()
def add(left: int, right: int) -> int:
    """Add two integers."""
    return left + right


@mcp.resource("demo://status")
def status() -> str:
    """Return a small status resource for smoke tests."""
    return "harness demo MCP server is available"


if __name__ == "__main__":
    mcp.run("stdio")
