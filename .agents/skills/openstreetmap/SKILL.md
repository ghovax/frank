---
id: openstreetmap
name: Create and update OpenStreetMap map artifacts
description: Use the configured OpenStreetMap MCP server to create and update interactive Leaflet/OpenStreetMap map artifacts for itineraries, stops, routes, flights, and GeoJSON overlays.
enabled: true
---

# Create and Update OpenStreetMap Map Artifacts

Use this skill when the user asks for an interactive map, travel itinerary map, path between places, route sketch, flight-style connection map, or geographic overlay.

## MCP workflow

The configured MCP server is `openstreetmap`. Start by discovering its current tools:

```json
{"server": "openstreetmap"}
```

Use `call_mcp_tool` with these tools:

- `create_map` — create a stateful map and return an HTML artifact. Pass explicit `places`; there are no default places.
- `update_map` — update an existing `map_id` with a replacement place list, title, mode, zoom, or GeoJSON overlay.
- `add_places` — append stops to an existing map.
- `list_maps` — inspect maps currently held by the stateful MCP subprocess.

The server is stateful, so keep and reuse the returned `map_id` when the user asks to modify a map. If a new turn lacks a known `map_id`, call `list_maps` before creating a duplicate.

## Artifact updates

The map tools use the harness-wide artifact update contract:

- `artifact_update_mode: "append"` renders a separate new artifact.
- `artifact_update_mode: "replace"` or `"update"` refreshes an existing artifact.
- `artifact_update_mode: "upsert"` refreshes the target when present, otherwise renders a new artifact.
- `artifact_target_id` selects the artifact to refresh; it defaults to the `map_id` for `update_map` and `add_places`.

When the user asks to modify an existing map, call `update_map` or `add_places` with the same `map_id` and the default replacement behavior. Use `append` only when the user asks to compare maps or keep both versions visible.

## Place shape

Each place is an object:

```json
{
  "label": "Florence",
  "latitude": 43.7696,
  "longitude": 11.2558,
  "description": "Museum stop"
}
```

Aliases accepted by the server: `name` for `label`, `lat` for `latitude`, and `lon`/`lng` for `longitude`.

## Modes

Use `mode` to communicate intent:

- `planning` for general map planning.
- `walking`, `driving`, or `rail` for ordered ground itineraries.
- `flight` for straight-line flight-style connections.

The current server draws ordered paths between places, computes approximate segment distances, and can overlay GeoJSON. It does not geocode addresses or call a live routing engine; if the user gives addresses without coordinates, get coordinates from an appropriate source first, then call the MCP server.
