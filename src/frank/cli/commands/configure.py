"""`frank configure`: read and change what the daemon and its sessions start with.

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

import logging
from typing import Any

from frank.base.configuration_file import (
    flatten as _flatten,
    load as _load,
    parse as _parse,
    read as _read,
    rejects as _validates,
    remove as _remove,
    save as _save,
    write as _write,
)
from frank.base.serialization import compact


logger = logging.getLogger("frank.configure")


def _known(path: str):
    """The schema's entry for a dotted path, or ``None`` if it defines none.

    Without this, a typo — or a setting that has since been removed, like the default agent —
    is written to the file, listed back, and silently does nothing. A setting that cannot take
    effect should be refused at the point it is set, not discovered when the behaviour never
    changes.

    Open-ended maps (`providers`, `mcp.servers`) accept any key at their level and are walked
    through into the model of their values, so `providers.anthropic.api_key` resolves."""
    from frank.base.configuration_schema import setting_for

    return setting_for(path)


def _validates(data: dict) -> str:
    """Whether the configuration would still load, and what is wrong if not.

    Checked before the file is written, because the daemon reads this file at startup: a value
    the schema rejects does not fail the command that set it, it fails every command after —
    including the one that would put it back."""
    from frank.base.configuration import Configuration

    try:
        Configuration.model_validate(data)
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
    from frank.base.configuration_schema import leaf_settings

    listing: dict[str, dict] = {}
    for setting in leaf_settings():
        try:
            current = _read(data, setting.path)
        except KeyError:
            current = setting.default
        entry: dict[str, Any] = {"default": setting.default, "current": current}
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
        logger.info("(what is set; `frank configure --all` lists every setting with its default)")
        return 0

    if arguments.value is None:
        known = _known(arguments.setting)
        try:
            value = _read(data, arguments.setting)
        except KeyError:
            if known is None:
                # A name the schema does not have will never do anything at all. Nothing on
                # stdout: there is no value to print, and a reader must not mistake an
                # explanation for one.
                logger.info(f"frank: no setting named {arguments.setting!r}")
                return 1
            # A real setting simply not in the file runs on what the code ships. Printing that
            # value rather than nothing is what makes reading a setting mean the same thing
            # whether or not somebody happened to write it down — and printing *only* it is what
            # makes `$(frank configure …)` in a script mean the value.
            value = known.default
        print(compact(value) if isinstance(value, (dict, list)) else value)
        return 0

    if _known(arguments.setting) is None:
        logger.info(f"frank: no setting named {arguments.setting!r} — it would be written and ignored")
        return 1
    _write(data, arguments.setting, _parse(arguments.value))
    invalid = _validates(data)
    if invalid:
        logger.info(f"frank: {arguments.setting} would not be valid: {invalid}")
        return 1
    _save(data)
    # Echoing the stored value rather than the argument shows how it was interpreted, so a
    # `true` that landed as a string is visible immediately instead of at the next boot.
    stored = _read(data, arguments.setting)
    print(compact(stored) if isinstance(stored, (dict, list)) else stored)
    return 0


def run_unset(arguments) -> int:
    data = _load()
    if not _remove(data, arguments.setting):
        logger.info(f"frank: no setting named {arguments.setting!r}")
        return 1
    invalid = _validates(data)
    if invalid:
        logger.info(f"frank: {arguments.setting} cannot be removed: {invalid}")
        return 1
    _save(data)
    # Nothing on stdout: removing a setting has no value to report, and the exit code already
    # says whether it happened.
    return 0
