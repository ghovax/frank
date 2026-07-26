"""`daisy configure`: read and change what the daemon and its sessions start with.

Settings live in one YAML file, and until now the only way to change them was to edit that
file by hand or open the desktop app. This exposes the same values to the terminal, addressed
by dotted path, so a setting can be inspected, changed, and scripted without either.

What it lists comes from the *schema*, not from the file. Reading the file could only ever
show what somebody had already written down — which is the part they know about — so every
setting left at its default was invisible, and the way to discover one was to read the source.
Walking the schema instead means a setting exists in the listing from the moment it exists in
the code, along with what it ships at and what it is for.

Changes apply to what starts *next*. A running session keeps the configuration it was built
with — the same guarantee its permission mode carries — so nothing here can reach into work
already in flight and change the rules underneath it.

Output is plumbing, like every other verb: a listing is a JSON object keyed by dotted path,
and reading one setting prints its value bare, with the explanation on stderr so a script
reading stdout never has to strip it. Values are printed as they are stored, credentials
included — this reads a file the user owns, and deciding on their behalf what they may see of
their own configuration is not this command's business.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import yaml

from daisy.base.paths import configuration_file_path
from daisy.base.serialization import compact


def _note(message: str) -> None:
    """A diagnostic. Never stdout — that carries the setting's value, and a reader must not
    have to tell one from the other."""
    print(message, file=sys.stderr)


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


def _known(path: str):
    """The schema's entry for a dotted path, or ``None`` if it defines none.

    Without this, a typo — or a setting that has since been removed, like the default agent —
    is written to the file, listed back, and silently does nothing. A setting that cannot take
    effect should be refused at the point it is set, not discovered when the behaviour never
    changes.

    Open-ended maps (`providers`, `mcp.servers`) accept any key at their level and are walked
    through into the model of their values, so `providers.anthropic.api_key` resolves."""
    from daisy.base.configuration_schema import setting_for

    return setting_for(path)


def _validates(data: dict) -> str:
    """Whether the configuration would still load, and what is wrong if not.

    Checked before the file is written, because the daemon reads this file at startup: a value
    the schema rejects does not fail the command that set it, it fails every command after —
    including the one that would put it back."""
    from daisy.base.configuration import GlobalConfiguration

    try:
        GlobalConfiguration.model_validate(data)
    except Exception as error:  # noqa: BLE001 — the validator's message is the useful part
        # Pydantic reports the field, then the reason, then a documentation URL. The first two
        # are what a person needs; the URL is noise at a terminal.
        lines = [line.strip() for line in str(error).splitlines()[1:3] if line.strip()]
        reason = " ".join(line for line in lines if not line.startswith("For further"))
        return reason.split(" [type=")[0] or str(error)
    return ""


def _everything(data: dict) -> dict:
    """Every setting the schema defines, each with what it is for, what it ships at, and what
    this machine currently runs on. The whole point of `--all`: what a person wants to know is
    what they *could* change, and that is a property of the code, not of their file."""
    from daisy.base.configuration_schema import leaf_settings

    listing: dict[str, dict] = {}
    for setting in leaf_settings():
        try:
            current = _read(data, setting.path)
        except KeyError:
            current = setting.default
        entry: dict[str, Any] = {"default": setting.default, "current": current}
        if setting.about:
            entry["about"] = setting.about
        if setting.open_ended:
            entry["open_ended"] = True
        listing[setting.path] = entry
    return listing


def run(arguments) -> int:
    data = _load()

    if getattr(arguments, "all", False):
        print(compact(_everything(data)))
        return 0

    if arguments.setting is None:
        # No argument: what this machine has actually been set to, as one object — the short
        # answer to "what have I changed?". `--all` is the long answer, and says so on stderr
        # rather than on stdout, which carries the values. A key the schema no longer defines
        # is inert; it is still listed, because it is in the file and hiding it would make an
        # unremovable setting invisible. `--unset` is how it goes away.
        print(compact(dict(sorted(_flatten(data)))))
        _note("(what is set; `daisy configure --all` lists every setting with its default)")
        return 0

    if arguments.value is None:
        known = _known(arguments.setting)
        try:
            value = _read(data, arguments.setting)
            source = "set in " + str(configuration_file_path())
        except KeyError:
            if known is None:
                # A name the schema does not have will never do anything at all. Nothing on
                # stdout: there is no value to print, and a reader must not mistake an
                # explanation for one.
                _note(f"daisy: no setting named {arguments.setting!r}")
                return 1
            # A real setting simply not in the file runs on what the code ships. Printing that
            # value rather than nothing is what makes reading a setting mean the same thing
            # whether or not somebody happened to write it down.
            value = known.default
            source = "default"
        print(compact(value) if isinstance(value, (dict, list)) else value)
        if known is not None and known.about:
            _note(f"{known.about} ({source})")
        else:
            _note(f"({source})")
        return 0

    if _known(arguments.setting) is None:
        _note(f"daisy: no setting named {arguments.setting!r} — it would be written and ignored")
        return 1
    _write(data, arguments.setting, _parse(arguments.value))
    invalid = _validates(data)
    if invalid:
        _note(f"daisy: {arguments.setting} would not be valid: {invalid}")
        return 1
    _save(data)
    # Echoing the stored value rather than the argument shows how it was interpreted, so a
    # `true` that landed as a string is visible immediately instead of at the next boot.
    stored = _read(data, arguments.setting)
    print(compact(stored) if isinstance(stored, (dict, list)) else stored)
    return 0


def run_unset(arguments) -> int:
    data = _load()
    parts = arguments.setting.split(".")
    node = data
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            _note(f"daisy: no setting named {arguments.setting!r}")
            return 1
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        _note(f"daisy: no setting named {arguments.setting!r}")
        return 1
    removed = node.pop(parts[-1])
    invalid = _validates(data)
    if invalid:
        node[parts[-1]] = removed
        _note(f"daisy: {arguments.setting} cannot be removed: {invalid}")
        return 1
    _save(data)
    # Nothing on stdout: removing a setting has no value to report, and the exit code already
    # says whether it happened.
    return 0
