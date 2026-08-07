"""Reading this machine: the XDG configuration, and the agents on disk."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langmesh.base.catalogue import project_catalogue
from langmesh.base.configuration import Configuration


def load_configuration(*, seed: bool = True) -> Configuration:
    """Read the XDG configuration file, seeding it from the packaged template on first run."""
    return Configuration.load(seed=seed)


def load_catalogue(configuration: Configuration, directory: str | Path) -> Any:
    """The agents, skills, memories and instructions reachable from `directory`."""
    return project_catalogue(configuration, str(Path(directory).resolve()))


def load_agent(
    name: str, directory: str | Path, *, configuration: Configuration | None = None
) -> Any:
    """One named agent profile from this machine, raising with what is available when the name is unknown."""
    resolved = configuration if configuration is not None else load_configuration(seed=False)
    catalogue = load_catalogue(resolved, directory)
    profile = catalogue.agent(name)
    if profile is None:
        available = ", ".join(catalogue.agents()) or "none"
        raise LookupError(f"No agent profile named {name!r}. This machine offers: {available}.")
    return profile


__all__ = ["load_agent", "load_catalogue", "load_configuration"]
