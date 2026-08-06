"""Reading and writing the configuration file, addressed by dotted path."""

from __future__ import annotations

import json
from typing import Any

import yaml

from langmesh.base.paths import configuration_file_path


def load() -> dict:
    """The configuration file as a plain document, or ``{}`` when there is none yet."""
    path = configuration_file_path()
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as error:
        raise RuntimeError(f"{path} is not valid YAML: {error}") from error


def save(data: dict) -> None:
    """Write the document back in the order it holds, so a person meets their settings as they wrote them."""
    path = configuration_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def flatten(data: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Every leaf as a dotted path, so settings can be addressed the way they are written."""
    if isinstance(data, dict):
        entries: list[tuple[str, Any]] = []
        for key, value in data.items():
            entries.extend(flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        return entries
    return [(prefix, data)]


def read(data: dict, path: str) -> Any:
    """The value at a dotted path, raising when the document holds none."""
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(path)
        node = node[part]
    return node


def write(data: dict, path: str, value: Any) -> None:
    """Set a dotted path, creating the objects above it. Mutates ``data`` in place."""
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        existing = node.get(part)
        if not isinstance(existing, dict):
            existing = {}
            node[part] = existing
        node = existing
    node[parts[-1]] = value


def remove(data: dict, path: str) -> bool:
    """Drop a dotted path and every object above it the drop left empty, answering whether anything was there."""
    parts = path.split(".")
    trail: list[tuple[dict, str]] = []
    node: Any = data
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return False
        trail.append((node, part))
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        return False
    del node[parts[-1]]
    for parent, key in reversed(trail):
        if parent[key] == {}:
            del parent[key]
    return True


def parse(raw: str) -> Any:
    """Interpret a value the way the file would hold it, so `true` lands as a boolean rather than a string."""
    lowered = raw.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "~"}:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def rejects(data: dict) -> str:
    """Why this document would not load, asked before the file is written because the daemon reads it at startup."""
    from langmesh.base.configuration import Configuration

    try:
        Configuration.model_validate(data)
    except Exception as error:  # noqa: BLE001 — the validator's message is the useful part
        # Pydantic reports the field, the reason and a documentation link, of which the link is noise here.
        lines = [line.strip() for line in str(error).splitlines()[1:3] if line.strip()]
        reason = " ".join(line for line in lines if not line.startswith("For further"))
        return reason.split(" [type=")[0] or str(error)
    return ""


__all__ = ["flatten", "load", "parse", "read", "rejects", "remove", "save", "write"]
