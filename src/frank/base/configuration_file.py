"""Reading and writing the configuration file, addressed by dotted path.

The file is one YAML document and a setting is a path into it, which is the same operation
whoever is asking: `frank configure workspace.strategy worktree` at a terminal, and the
settings panel writing the same path over the socket. This module is that operation, once.

It was the terminal's alone, as private helpers inside the command. The interface then grew a
second way to change settings — an endpoint per toggle, each naming its own field, each
persisting through its own keyword argument — and the two drifted in the way two
implementations of one thing do: the terminal validated the whole document before writing and
the endpoints did not, so a value the schema rejects could be written by one route and then
stop the daemon reading it back. What is here validates first, whoever is writing.

**Changes apply to what starts next.** A running session keeps the configuration it was built
with, which is the same guarantee its permission mode carries: nothing written here reaches
into work already in flight and changes the rules underneath it.
"""

from __future__ import annotations

import json
from typing import Any

import yaml

from frank.base.paths import configuration_file_path


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
    """Write the document back, in the order it holds rather than alphabetically — a person
    reading their own file should meet the settings in the order they wrote them."""
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
    """The value at a dotted path. Raises ``KeyError`` when the document does not hold one,
    which is different from holding an empty one and is what tells a caller to fall back to the
    schema's default rather than to report a blank."""
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
    """Drop a dotted path, and every object above it that the drop left empty. Answers whether
    anything was there — putting a setting back is removing it, and a caller that cannot tell
    "removed" from "was not set" cannot report either honestly."""
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
    """Interpret a value the way the file would hold it.

    A setting written as `true` or `8` should land as a boolean or a number, not as the string
    the shell handed over — otherwise a toggle set from the terminal reads as truthy text and
    can never be turned off.

    `none` is deliberately *not* one of the null spellings, even though YAML accepts it as one.
    It is a real value here — `workspace.strategy: none` is the default — and coercing it to
    null wrote a configuration the schema rejects, which stopped the daemon from starting at
    all. Neither is the empty string: most settings that can be blank are typed as strings, so
    coercing `""` to null made clearing one impossible — it was refused by the very schema the
    blank value satisfies. Removing a setting is what `--unset` is for; `null` and `~` still
    spell null for the fields that genuinely take one."""
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
    """Why this document would not load, or ``""`` when it would.

    Asked before the file is written, because the daemon reads it at startup: a value the schema
    rejects does not fail the command that set it, it fails every command after — including the
    one that would put it back."""
    from frank.base.configuration import Configuration

    try:
        Configuration.model_validate(data)
    except Exception as error:  # noqa: BLE001 — the validator's message is the useful part
        # Pydantic reports the field, then the reason, then a documentation URL.
        lines = [line.strip() for line in str(error).splitlines()[1:3] if line.strip()]
        reason = " ".join(line for line in lines if not line.startswith("For further"))
        return reason.split(" [type=")[0] or str(error)
    return ""


__all__ = ["flatten", "load", "parse", "read", "rejects", "remove", "save", "write"]
