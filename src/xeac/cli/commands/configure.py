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
import sys
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
    can never be turned off.

    `none` is deliberately *not* one of the null spellings, even though YAML accepts it as one.
    It is a real value here — `workspace.strategy: none` is the default — and coercing it to
    null wrote a configuration the schema rejects, which stopped the daemon from starting at
    all. Removing a setting is what `--unset` is for; `null` and `~` still spell null for the
    fields that genuinely take one."""
    lowered = raw.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "~", ""}:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _is_known(path: str) -> bool:
    """Whether the schema defines this dotted path.

    Without this, a typo — or a setting that has since been removed, like the default agent —
    is written to the file, listed back, and silently does nothing, because a configuration
    model ignores keys it does not know. A setting that cannot take effect should be refused
    at the point it is set, not discovered when the behaviour never changes.

    Open-ended maps (`providers`, `mcp.servers`) accept any key at their level and are walked
    through into the model of their values, so `providers.anthropic.api_key` resolves."""
    import typing

    from pydantic import BaseModel

    from xeac.base.configuration import GlobalConfiguration

    def descend(annotation: Any, segments: list[str]) -> bool:
        if not segments:
            return True
        origin = typing.get_origin(annotation)
        if origin is dict:
            # Any key is valid here; the value type decides what may follow it.
            value_type = typing.get_args(annotation)[1]
            return descend(value_type, segments[1:])
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            field = annotation.model_fields.get(segments[0])
            if field is None:
                return False
            return descend(field.annotation, segments[1:])
        # A scalar: nothing may follow it.
        return False

    return descend(GlobalConfiguration, path.split("."))


def _validates(data: dict) -> str:
    """Whether the configuration would still load, and what is wrong if not.

    Checked before the file is written, because the daemon reads this file at startup: a value
    the schema rejects does not fail the command that set it, it fails every command after —
    including the one that would put it back."""
    from xeac.base.configuration import GlobalConfiguration

    try:
        GlobalConfiguration.model_validate(data)
    except Exception as error:  # noqa: BLE001 — the validator's message is the useful part
        # Pydantic reports the field, then the reason, then a documentation URL. The first two
        # are what a person needs; the URL is noise at a terminal.
        lines = [line.strip() for line in str(error).splitlines()[1:3] if line.strip()]
        reason = " ".join(line for line in lines if not line.startswith("For further"))
        return reason.split(" [type=")[0] or str(error)
    return ""


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
            # A key the schema no longer defines is inert. Saying so is the difference between
            # a setting that does nothing and a setting that appears to be doing something.
            stale = "" if _is_known(path) else "   (not a setting; ignored)"
            print(f"{path.ljust(width)}  {shown}{stale}")
        return 0

    if arguments.value is None:
        try:
            value = _read(data, arguments.setting)
        except KeyError:
            # Two different absences, and conflating them was confusing: a real setting that
            # simply is not in the file runs on the schema's default, while a name the schema
            # does not have will never do anything at all.
            if _is_known(arguments.setting):
                print(f"{arguments.setting} is not set; its built-in default applies")
                return 0
            print(f"xeac: no setting named {arguments.setting!r}", file=sys.stderr)
            return 1
        shown = _mask(value) if _is_secret(arguments.setting) else value
        print(json.dumps(shown, indent=2) if isinstance(shown, (dict, list)) else shown)
        return 0

    if arguments.unset:
        print("xeac: pass either a value or --unset, not both")
        return 1

    if not _is_known(arguments.setting):
        print(
            f"xeac: no setting named {arguments.setting!r} — it would be written and ignored",
            file=sys.stderr,
        )
        return 1
    _write(data, arguments.setting, _parse(arguments.value))
    invalid = _validates(data)
    if invalid:
        print(f"xeac: {arguments.setting} would not be valid: {invalid}", file=sys.stderr)
        return 1
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
    removed = node.pop(parts[-1])
    invalid = _validates(data)
    if invalid:
        node[parts[-1]] = removed
        print(f"xeac: {arguments.setting} cannot be removed: {invalid}", file=sys.stderr)
        return 1
    _save(data)
    print(f"unset {arguments.setting}")
    return 0
