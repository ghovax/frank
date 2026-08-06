#!/usr/bin/env python3
"""Emit the JSON Schema for the wire events and model-facing envelopes, from Pydantic as the source of truth."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pydantic import TypeAdapter  # noqa: E402
from pydantic.json_schema import models_json_schema  # noqa: E402

from langmesh.protocol import events  # noqa: E402

logger = logging.getLogger(__name__)

# `shared/` rather than `web/`, since both clients read this union and two copies would drift.
OUTPUT = ROOT / "shared" / "generated" / "events.schema.json"
REFERENCE_TEMPLATE = "#/$defs/{model}"


def _strip_titles(node: object) -> object:
    """Drop Pydantic's injected title metadata, which would otherwise promote every field into a throwaway alias."""
    if isinstance(node, list):
        return [_strip_titles(item) for item in node]
    if not isinstance(node, dict):
        return node
    cleaned: dict = {}
    for key, value in node.items():
        if key == "title":
            # Metadata on this node rather than a field name, since field names are reached only as map keys below.
            continue
        if key in _SCHEMA_MAP_KEYS and isinstance(value, dict):
            cleaned[key] = {name: _strip_titles(sub) for name, sub in value.items()}
        elif key in _SCHEMA_LIST_KEYS and isinstance(value, list):
            cleaned[key] = [_strip_titles(sub) for sub in value]
        elif key in _SCHEMA_NODE_KEYS and isinstance(value, dict):
            cleaned[key] = _strip_titles(value)
        else:
            cleaned[key] = _strip_titles(value)
    return cleaned


_STRUCTURAL = {"type", "$ref", "enum", "const", "anyOf", "oneOf", "allOf", "items", "properties", "tsType"}
# The keywords whose values are themselves schemas, so the walk descends only into real schema positions.
_SCHEMA_MAP_KEYS = {"properties", "$defs", "definitions", "patternProperties"}
_SCHEMA_LIST_KEYS = {"anyOf", "allOf", "oneOf", "prefixItems"}
_SCHEMA_NODE_KEYS = {"items", "additionalProperties", "not", "if", "then", "else"}


def _readable_types(node: object) -> object:
    """Give the generator clean types instead of anonymous index signatures everywhere."""
    if not isinstance(node, dict):
        return node
    if node.get("type") == "object" and node.get("additionalProperties") is True and "properties" not in node:
        return {"tsType": "Record<string, unknown>"}
    if not (_STRUCTURAL & set(node.keys())):
        keep = {key: node[key] for key in ("description", "default") if key in node}
        return {**keep, "tsType": "unknown"}
    cleaned: dict = {}
    for key, value in node.items():
        if key in _SCHEMA_MAP_KEYS and isinstance(value, dict):
            cleaned[key] = {name: _readable_types(sub) for name, sub in value.items()}
        elif key in _SCHEMA_LIST_KEYS and isinstance(value, list):
            cleaned[key] = [_readable_types(sub) for sub in value]
        elif key in _SCHEMA_NODE_KEYS and isinstance(value, dict):
            cleaned[key] = _readable_types(value)
        else:
            cleaned[key] = value
    if cleaned.get("type") == "object" and "properties" in cleaned and "additionalProperties" not in cleaned:
        cleaned["additionalProperties"] = False
    return cleaned


def _require_discriminant(definition: object) -> object:
    """Mark the discriminant required, since Pydantic makes it optional and the union would not narrow."""
    if not isinstance(definition, dict):
        return definition
    properties = definition.get("properties")
    if isinstance(properties, dict) and "kind" in properties:
        required = list(definition.get("required") or [])
        if "kind" not in required:
            required.append("kind")
        definition["required"] = required
    return definition


def _render_schema() -> str:
    top_level = [
        *events.WIRE_EVENT_MODELS,
        events.ModelToolResult,
        events.TurnContext,
    ]
    _, combined = models_json_schema(
        [(model, "validation") for model in top_level],
        # `ref_template` is pydantic's own keyword argument — its name is fixed API.
        ref_template=REFERENCE_TEMPLATE,
    )
    definitions: dict = combined.get("$defs", {})

    # Expose the discriminated union as a named type, which the generator renders from the `oneOf`.
    wire = TypeAdapter(events.WireEvent).json_schema(ref_template=REFERENCE_TEMPLATE)
    definitions.update(wire.pop("$defs", {}))
    definitions["WireEvent"] = wire

    cleaned = {name: _require_discriminant(_readable_types(_strip_titles(definition))) for name, definition in definitions.items()}
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "LangMeshEvents",
        # The root document only exists to carry its definitions, so closing it keeps a stray wrapper out.
        "type": "object",
        "additionalProperties": False,
        "$defs": cleaned,
    }
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="Verify the committed schema matches the Python models; exit 1 on drift instead of writing.",
    )
    arguments = parser.parse_args()
    rendered = _render_schema()

    if arguments.check:
        current = OUTPUT.read_text() if OUTPUT.exists() else ""
        if current != rendered:
            logger.error(
                "%s is stale — the Pydantic event models changed but the schema was not regenerated. "
                "Run `bun run build:events` and commit the result.",
                OUTPUT.relative_to(ROOT),
            )
            return 1
        logger.info("%s is up to date with the event models.", OUTPUT.relative_to(ROOT))
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered)
    logger.info("wrote %s", OUTPUT.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
