import asyncio
import json
import math
import sys
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP


mcp = FastMCP("openstreetmap")

TEMPLATE_PATH = Path(__file__).with_name("map.html")
MAPS: dict[str, dict[str, Any]] = {}
ARTIFACT_UPDATE_MODES = {"append", "replace", "update", "upsert"}


def _place_label(place: dict[str, Any], index: int) -> str:
    return str(place.get("label") or place.get("name") or f"Stop {index + 1}")


def _coordinate(place: dict[str, Any]) -> tuple[float, float]:
    latitude = place.get("latitude", place.get("lat"))
    longitude = place.get("longitude", place.get("lon", place.get("lng")))
    if latitude is None or longitude is None:
        raise ValueError("Each place must include latitude/lat and longitude/lon/lng.")
    return (float(latitude), float(longitude))


def _normalize_places(places: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized = []
    for index, place in enumerate(places or []):
        latitude, longitude = _coordinate(place)
        normalized.append({
            "label": _place_label(place, index),
            "latitude": latitude,
            "longitude": longitude,
            "description": str(place.get("description") or ""),
        })
    return normalized


def _haversine_km(first: dict[str, Any], second: dict[str, Any]) -> float:
    radius_km = 6371.0
    first_latitude = math.radians(float(first["latitude"]))
    second_latitude = math.radians(float(second["latitude"]))
    delta_latitude = second_latitude - first_latitude
    delta_longitude = math.radians(float(second["longitude"]) - float(first["longitude"]))
    a = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(first_latitude) * math.cos(second_latitude) * math.sin(delta_longitude / 2) ** 2
    )
    return 2 * radius_km * math.asin(math.sqrt(a))


def _segments(places: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "from": places[index]["label"],
            "to": places[index + 1]["label"],
            "distance_km": round(_haversine_km(places[index], places[index + 1]), 1),
        }
        for index in range(len(places) - 1)
    ]


def _json_for_template(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True).replace("</", "<\\/")


def _render_html(map_data: dict[str, Any]) -> str:
    template = TEMPLATE_PATH.read_text()
    return template.replace("__MAP_DATA_JSON__", _json_for_template(map_data))


def _summarize(map_data: dict[str, Any]) -> dict[str, Any]:
    segments = _segments(map_data["places"]) if map_data["connect_places"] else []
    return {
        "map_id": map_data["map_id"],
        "title": map_data["title"],
        "mode": map_data["mode"],
        "place_count": len(map_data["places"]),
        "segment_count": len(segments),
        "distance_km": round(sum(segment["distance_km"] for segment in segments), 1),
        "segments": segments,
    }


def _artifact_mode(value: str, default: str) -> str:
    normalized = (value or default).strip().lower()
    if normalized == "new":
        return "append"
    return normalized if normalized in ARTIFACT_UPDATE_MODES else default


def _artifact(
    map_data: dict[str, Any],
    artifact_update_mode: str = "append",
    artifact_target_id: str = "",
) -> dict[str, Any]:
    summary = _summarize(map_data)
    target_id = artifact_target_id.strip()
    mode = _artifact_mode(artifact_update_mode, "append")
    return {
        "context": {
            **summary,
            "places": map_data["places"],
            "summary": f"{map_data['title']}: {summary['place_count']} places, {summary['segment_count']} path segments.",
        },
        "artifacts": [
            {
                "artifact_id": map_data["map_id"],
                "artifact_target_id": target_id or map_data["map_id"],
                "artifact_update_mode": mode,
                "type": "html",
                "title": map_data["title"],
                "html": _render_html({**map_data, "segments": summary["segments"]}),
                "height": 540,
                "summary": "Interactive Leaflet map using OpenStreetMap tiles.",
                "sandbox": "allow-scripts allow-same-origin allow-popups",
            }
        ],
    }


async def _progress(context: Context | None, step: int, total: int, message: str) -> None:
    if context is None:
        return
    await context.report_progress(step, total, message)
    await asyncio.sleep(0.03)


@mcp.tool()
async def create_map(
    places: list[dict[str, Any]],
    title: str = "OpenStreetMap itinerary",
    mode: str = "planning",
    connect_places: bool = True,
    zoom: int = 13,
    geojson: dict[str, Any] | None = None,
    map_id: str = "",
    artifact_update_mode: str = "append",
    artifact_target_id: str = "",
    context: Context | None = None,
) -> dict[str, Any]:
    """Create a stateful interactive OpenStreetMap map and return its artifact.

    Args:
        places: Stops or points to show. Each item accepts label/name, latitude/lat,
            longitude/lon/lng, and optional description.
        title: Map title displayed above the artifact.
        mode: Visual route mode, such as planning, walking, driving, rail, or flight.
        connect_places: Whether to draw a path through places in order.
        zoom: Maximum zoom used when fitting the viewport.
        geojson: Optional GeoJSON feature collection or geometry overlay.
        map_id: Optional stable identifier; generated when omitted.
        artifact_update_mode: append to render a new artifact, replace/update to
            refresh a target artifact, or upsert to replace when present.
        artifact_target_id: Existing artifact id to refresh; defaults to map_id.
    """
    await _progress(context, 1, 4, "Validating map places")
    normalized_places = _normalize_places(places)
    identifier = map_id.strip() or f"map-{uuid.uuid4().hex[:10]}"
    await _progress(context, 2, 4, f"Creating {identifier}")
    MAPS[identifier] = {
        "map_id": identifier,
        "title": title,
        "mode": mode,
        "places": normalized_places,
        "connect_places": bool(connect_places),
        "zoom": max(1, min(19, int(zoom))),
        "geojson": geojson,
    }
    await _progress(context, 3, 4, "Rendering map artifact")
    result = _artifact(MAPS[identifier], artifact_update_mode, artifact_target_id)
    await _progress(context, 4, 4, "Map ready")
    return result


@mcp.tool()
async def update_map(
    map_id: str,
    places: list[dict[str, Any]] | None = None,
    title: str | None = None,
    mode: str | None = None,
    connect_places: bool | None = None,
    zoom: int | None = None,
    geojson: dict[str, Any] | None = None,
    replace_geojson: bool = False,
    artifact_update_mode: str = "replace",
    artifact_target_id: str = "",
    context: Context | None = None,
) -> dict[str, Any]:
    """Update an existing map and return the refreshed artifact."""
    if map_id not in MAPS:
        raise ValueError(f"Unknown map_id: {map_id}")
    await _progress(context, 1, 3, f"Loading {map_id}")
    map_data = dict(MAPS[map_id])
    if places is not None:
        map_data["places"] = _normalize_places(places)
    if title is not None:
        map_data["title"] = title
    if mode is not None:
        map_data["mode"] = mode
    if connect_places is not None:
        map_data["connect_places"] = bool(connect_places)
    if zoom is not None:
        map_data["zoom"] = max(1, min(19, int(zoom)))
    if geojson is not None or replace_geojson:
        map_data["geojson"] = geojson
    await _progress(context, 2, 3, "Applying map updates")
    MAPS[map_id] = map_data
    result = _artifact(map_data, artifact_update_mode, artifact_target_id or map_id)
    await _progress(context, 3, 3, "Updated map ready")
    return result


@mcp.tool()
async def add_places(
    map_id: str,
    places: list[dict[str, Any]],
    artifact_update_mode: str = "replace",
    artifact_target_id: str = "",
    context: Context | None = None,
) -> dict[str, Any]:
    """Append places to an existing map and return the refreshed artifact."""
    if map_id not in MAPS:
        raise ValueError(f"Unknown map_id: {map_id}")
    await _progress(context, 1, 3, f"Loading {map_id}")
    map_data = dict(MAPS[map_id])
    map_data["places"] = [*map_data["places"], *_normalize_places(places)]
    await _progress(context, 2, 3, f"Added {len(places)} places")
    MAPS[map_id] = map_data
    result = _artifact(map_data, artifact_update_mode, artifact_target_id or map_id)
    await _progress(context, 3, 3, "Updated map ready")
    return result


@mcp.tool()
def list_maps() -> dict[str, Any]:
    """List maps currently held by this stateful MCP server process."""
    return {
        "maps": [_summarize(map_data) for map_data in MAPS.values()],
        "count": len(MAPS),
    }


if __name__ == "__main__":
    transport = "streamable-http" if "--http" in sys.argv else "stdio"
    mcp.run(transport)
