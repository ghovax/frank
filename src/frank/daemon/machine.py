"""Reading this machine: the XDG configuration, and the agents on disk.

This is the daemon's business and nothing else's. `frankd` is the program that runs on a
machine — it owns the databases, it reads the configuration, and the person running it is the
person whose home directory it reads. The library underneath has no such licence: it is a
shell around what you pass it, and it never goes looking.

So the layering runs one way:

- `frank.Session` — the harness. Everything it needs is a constructor argument. It reads no
  path it was not given and resolves no name against anything.
- This module — the machine. It turns a home directory into the objects `Session` takes.
- `frankd`, and the CLI — programs built on both, for a person on a machine.

A caller who wants what the daemon has can import this deliberately. That is the point: the
line is visible, and it is theirs to cross.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from frank.base.catalogue import project_catalogue
from frank.base.configuration import GlobalConfiguration


def load_configuration(*, seed: bool = True) -> GlobalConfiguration:
    """Read the XDG configuration file, seeding it from the packaged template on first run.

    `seed=False` reads without creating, for a program that wants what is there and does not
    want to leave a file behind if there is nothing.
    """
    return GlobalConfiguration.load(seed=seed)


def load_catalogue(configuration: GlobalConfiguration, directory: str | Path) -> Any:
    """The agents, skills, memories and instructions reachable from `directory`.

    Walks that directory's `.agents`, the packaged base layer, and — because this is the
    daemon's side of the line — the home directory too.
    """
    return project_catalogue(configuration, str(Path(directory).resolve()))


def load_agent(name: str, directory: str | Path, *, configuration: GlobalConfiguration | None = None) -> Any:
    """One named agent profile from this machine, ready to hand to `Session`.

    Raises `LookupError` naming what is available, because a typo in an agent name is the most
    common way this fails and the list is what you need to fix it.
    """
    resolved = configuration if configuration is not None else load_configuration(seed=False)
    catalogue = load_catalogue(resolved, directory)
    profile = catalogue.agent(name)
    if profile is None:
        available = ", ".join(catalogue.agents()) or "none"
        raise LookupError(f"No agent profile named {name!r}. This machine offers: {available}.")
    return profile


__all__ = ["load_agent", "load_catalogue", "load_configuration"]
