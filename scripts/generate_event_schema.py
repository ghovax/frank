#!/usr/bin/env python3
"""Emit the JSON Schema for the wire events and model-facing envelopes.

Pydantic is the source of truth (``harness.core.events``); this uses Pydantic's own
``models_json_schema`` — no hand-written Python->TS type mapping — to produce
``web/src/lib/generated/events.schema.json``. The TypeScript is then generated from
that schema by ``json-schema-to-typescript`` (see the web ``gen:events`` script), so
the whole pipeline is two authoritative libraries and zero bespoke type-walking.

Run the full pipeline with ``bun run gen:events`` in ``web/`` (which invokes this and
then ``json2ts``); this file alone only refreshes the schema.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pydantic import TypeAdapter  # noqa: E402
from pydantic.json_schema import models_json_schema  # noqa: E402

from harness.core import events  # noqa: E402

logger = logging.getLogger(__name__)

OUTPUT = ROOT / "web" / "src" / "lib" / "generated" / "events.schema.json"
REFERENCE_TEMPLATE = "#/$defs/{model}"


def _strip_titles(node: object) -> object:
    """Drop Pydantic's per-field ``title`` keys. They cause json-schema-to-typescript
    to promote every field into its own throwaway alias (Kind1, Path4, …); the type
    names we want come from the ``$defs`` keys, not titles."""
    if isinstance(node, dict):
        return {key: _strip_titles(value) for key, value in node.items() if key != "title"}
    if isinstance(node, list):
        return [_strip_titles(item) for item in node]
    return node


_STRUCTURAL = {"type", "$ref", "enum", "const", "anyOf", "oneOf", "allOf", "items", "properties", "tsType"}
# JSON-Schema keywords whose *values* are themselves schemas (or maps/lists of them),
# so the walk descends only into real schema positions — never into a data map like
# ``properties`` (keyed by field name) or a list like ``required``.
_SCHEMA_MAP_KEYS = {"properties", "$defs", "definitions", "patternProperties"}
_SCHEMA_LIST_KEYS = {"anyOf", "allOf", "oneOf", "prefixItems"}
_SCHEMA_NODE_KEYS = {"items", "additionalProperties", "not", "if", "then", "else"}


def _readable_types(node: object) -> object:
    """Give json-schema-to-typescript clean TS instead of anonymous ``{ [k: string]:
    unknown }`` index signatures everywhere:

    * ``dict[str, Any]`` (``type: object`` + open ``additionalProperties``) -> the named
      ``Record<string, unknown>`` (via json2ts's ``tsType`` extension keyword);
    * a free ``Any`` schema -> ``unknown``;
    * every model object -> ``additionalProperties: false`` so its interface is closed
      and gains no stray index signature at all.
    """
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


def main() -> None:
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

    # Expose the discriminated union as a named `WireEvent` type. Pydantic emits it as
    # a `oneOf` + discriminator; json-schema-to-typescript renders that as a TS union.
    wire = TypeAdapter(events.WireEvent).json_schema(ref_template=REFERENCE_TEMPLATE)
    definitions.update(wire.pop("$defs", {}))
    definitions["WireEvent"] = wire

    cleaned = {name: _readable_types(_strip_titles(definition)) for name, definition in definitions.items()}
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "DaisyEvents",
        # The root doc only exists to carry `$defs`; closing it keeps json2ts from
        # emitting a stray open `DaisyEvents { [key: string]: unknown }` wrapper.
        "type": "object",
        "additionalProperties": False,
        "$defs": cleaned,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    logger.info("wrote %s (%d definitions)", OUTPUT.relative_to(ROOT), len(definitions))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    main()
