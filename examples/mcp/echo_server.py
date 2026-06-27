from urllib.parse import quote_plus

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


@mcp.tool()
def openstreetmap_view(
    latitude: float,
    longitude: float,
    label: str = "Map",
    zoom: int = 14,
) -> dict:
    """Return a renderable OpenStreetMap viewer artifact for a coordinate."""
    bounded_zoom = max(1, min(19, zoom))
    delta = max(0.002, 0.35 / bounded_zoom)
    south = latitude - delta
    north = latitude + delta
    west = longitude - delta
    east = longitude + delta
    marker = quote_plus(f"{latitude},{longitude}")
    iframe_src = (
        "https://www.openstreetmap.org/export/embed.html"
        f"?bbox={west}%2C{south}%2C{east}%2C{north}"
        f"&layer=mapnik&marker={marker}"
    )
    return {
        "context": {
            "summary": f"OpenStreetMap viewer for {label}.",
            "latitude": latitude,
            "longitude": longitude,
            "zoom": bounded_zoom,
        },
        "artifacts": [
            {
                "type": "iframe",
                "title": label,
                "src": iframe_src,
                "height": 420,
                "summary": f"Interactive map centered on {label}.",
                "sandbox": "allow-scripts allow-same-origin allow-popups",
            }
        ],
    }


@mcp.resource("demo://status")
def status() -> str:
    """Return a small status resource for smoke tests."""
    return "harness demo MCP server is available"


if __name__ == "__main__":
    mcp.run("stdio")
