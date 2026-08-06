"""`frank configure`: read and change what the daemon and its sessions start with."""

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
    """The schema's entry for a dotted path, or ``None`` if it defines none."""
    from frank.base.configuration_schema import setting_for

    return setting_for(path)


def _everything(data: dict) -> dict:
    """Every setting the schema defines, with what it ships at and what this machine currently runs on."""
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
        # No argument: what this machine has actually been set to, as one object — the short answer to "what have I changed?".
        print(compact(dict(sorted(_flatten(data)))))
        logger.info("(what is set; `frank configure --all` lists every setting with its default)")
        return 0

    if arguments.value is None:
        known = _known(arguments.setting)
        try:
            value = _read(data, arguments.setting)
        except KeyError:
            if known is None:
                # A name the schema does not have will never do anything at all.
                logger.info(f"frank: no setting named {arguments.setting!r}")
                return 1
            # A real setting simply not in the file runs on what the code ships.
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
    # Echoing the stored value rather than the argument shows how it was interpreted, so a `true` that landed as a string is visible immediately instead of at the next boot.
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
    # Nothing on stdout: removing a setting has no value to report, and the exit code already says whether it happened.
    return 0
