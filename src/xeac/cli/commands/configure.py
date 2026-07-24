"""`xeac configure`: read and change what the daemon and its sessions start with.

Settings live in one YAML file, and until now the only way to change them was to edit that
file by hand or open the desktop app. This exposes the same values to the terminal, addressed
by dotted path, so a setting can be inspected, changed, and scripted without either.

Changes apply to what starts *next*. A running session keeps the configuration it was built
with — the same guarantee its permission mode carries — so nothing here can reach into work
already in flight and change the rules underneath it.
"""

from __future__ import annotations

import json
from typing import Any

import yaml

from xeac.base.paths import configuration_file_path

# Values that must never be printed in full. A configuration dump is something people paste
# into issues, and an API key that survives that trip is a leaked API key.
_SECRET_MARKERS = ("api_key", "token", "secret", "password")


def _is_secret(path: str) -> bool:
    leaf = path.rsplit(".", 1)[-1].lower()
    return any(marker in leaf for marker in _SECRET_MARKERS)


def _mask(value: Any) -> Any:
    """A secret, shown as evidence that it is set without disclosing it."""
    text = str(value)
    if not text:
        return ""
    return f"…{text[-4:]} (set)" if len(text) > 4 else "(set)"


def _load() -> dict:
    path = configuration_file_path()
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as error:
        raise RuntimeError(f"{path} is not valid YAML: {error}") from error


def _save(data: dict) -> None:
    path = configuration_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _flatten(data: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Every leaf as a dotted path, so settings can be addressed the way they are written."""
    if isinstance(data, dict):
        entries: list[tuple[str, Any]] = []
        for key, value in data.items():
            entries.extend(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        return entries
    return [(prefix, data)]


def _read(data: dict, path: str) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(path)
        node = node[part]
    return node


def _write(data: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        existing = node.get(part)
        if not isinstance(existing, dict):
            existing = {}
            node[part] = existing
        node = existing
    node[parts[-1]] = value


def _parse(raw: str) -> Any:
    """Interpret a value the way the file would hold it.

    A setting written as `true` or `8` should land as a boolean or a number, not as the string
    the shell handed over — otherwise a toggle set from the terminal reads as truthy text and
    can never be turned off."""
    lowered = raw.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none", ""}:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def run(arguments) -> int:
    data = _load()

    if arguments.setting is None:
        # No argument: show everything, so a person can discover what there is to change.
        entries = sorted(_flatten(data))
        if arguments.json:
            print(json.dumps({
                path: (_mask(value) if _is_secret(path) else value) for path, value in entries
            }, indent=2))
            return 0
        if not entries:
            print(f"no settings yet ({configuration_file_path()})")
            return 0
        width = max(len(path) for path, _ in entries)
        for path, value in entries:
            shown = _mask(value) if _is_secret(path) else value
            print(f"{path.ljust(width)}  {shown}")
        return 0

    if arguments.value is None:
        try:
            value = _read(data, arguments.setting)
        except KeyError:
            print(f"xeac: no setting named {arguments.setting!r}")
            return 1
        shown = _mask(value) if _is_secret(arguments.setting) else value
        print(json.dumps(shown, indent=2) if isinstance(shown, (dict, list)) else shown)
        return 0

    if arguments.unset:
        print("xeac: pass either a value or --unset, not both")
        return 1

    _write(data, arguments.setting, _parse(arguments.value))
    _save(data)
    # Echoing the stored value rather than the argument shows how it was interpreted, so a
    # `true` that landed as a string is visible immediately instead of at the next boot.
    stored = _read(data, arguments.setting)
    print(f"{arguments.setting} = {_mask(stored) if _is_secret(arguments.setting) else stored}")
    print("applies to sessions and daemons started from now on")
    return 0


def run_unset(arguments) -> int:
    data = _load()
    parts = arguments.setting.split(".")
    node = data
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            print(f"xeac: no setting named {arguments.setting!r}")
            return 1
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        print(f"xeac: no setting named {arguments.setting!r}")
        return 1
    node.pop(parts[-1])
    _save(data)
    print(f"unset {arguments.setting}")
    return 0
