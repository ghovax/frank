"""The tools a session installs for itself, kept apart from what the machine happens to have."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from langmesh.base import environment_variables
from langmesh.base.paths import session_toolbox_directory, session_toolboxes_directory

# What the environment has to say for the ordinary command to mean this session's own.
_XDG_STATE = environment_variables.XDG_STATE_HOME
_NIX_CONFIG = environment_variables.NIX_CONFIG
# Additive rather than replacing, so a cache or substituter the person configured keeps working.
_XDG_SETTING = "use-xdg-base-directories = true"


@dataclass(frozen=True)
class Toolbox:
    """One session's own tools: where they live, and what a child needs to find them."""

    session_id: str
    root: Path

    @property
    def profile(self) -> Path:
        """The profile directory the package manager maintains for this session."""
        return self.root / "nix" / "profile"

    @property
    def binaries(self) -> Path:
        """What goes on `PATH`, which need not exist until the session installs something."""
        return self.profile / "bin"

    def environment(self, inherited: Optional[dict[str, str]] = None) -> dict[str, str]:
        """The environment a tool child needs, with `PATH` prepended rather than replaced."""
        base = dict(inherited if inherited is not None else os.environ)
        existing = base.get("PATH", "")
        return {
            "PATH": f"{self.binaries}:{existing}" if existing else str(self.binaries),
            _XDG_STATE: str(self.root),
            _NIX_CONFIG: _joined_config(base.get(_NIX_CONFIG, "")),
        }

    def prepare(self) -> "Toolbox":
        """Make sure the directory exists, called when tools are first wired up rather than at session creation."""
        self.root.mkdir(parents=True, exist_ok=True)
        return self


def _joined_config(existing: str) -> str:
    """Our one setting, added to whatever the parent already asked for."""
    lines = [line for line in existing.splitlines() if line.strip()]
    if _XDG_SETTING not in lines:
        lines.append(_XDG_SETTING)
    return "\n".join(lines)


@lru_cache(maxsize=1)
def available() -> bool:
    """Whether this machine can give a session tools of its own, cached because it is a fact about the machine."""
    return shutil.which("nix") is not None


def toolbox_for(session_id: str, *, enabled: bool = True) -> Optional[Toolbox]:
    """This session's toolbox, or `None`, which is an answer rather than a degraded toolbox."""
    if not enabled or not session_id or not available():
        return None
    return Toolbox(session_id=session_id, root=session_toolbox_directory(session_id))


def discard(session_id: str) -> None:
    """Delete a session's toolbox because the session is over, leaving the shared store's packages alone."""
    if not session_id:
        return
    root = session_toolbox_directory(session_id)
    shutil.rmtree(root, ignore_errors=True)


def sweep(live_session_ids: Iterable[str]) -> list[str]:
    """Delete the toolboxes of sessions that are gone, and answer with what went."""
    root = session_toolboxes_directory()
    if not root.is_dir():
        return []
    live = set(live_session_ids)
    swept = []
    for entry in root.iterdir():
        if entry.is_dir() and entry.name not in live:
            shutil.rmtree(entry, ignore_errors=True)
            swept.append(entry.name)
    return swept


__all__ = ["Toolbox", "available", "discard", "sweep", "toolbox_for"]
