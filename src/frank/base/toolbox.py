"""The tools a session installs for itself.

A session arrives with whatever the machine happens to have, and the sandbox decides what it may
reach. Those are different questions, and until this existed they collided into one answer: a
missing tool and a forbidden path both came back as ``Operation not permitted``, so a gap in the
toolkit read as a policy fight — and a model that reads a policy fight goes looking for a way
around it, which is the behaviour nobody wants and everybody has seen.

A toolbox is the honest answer to the first question. Each session gets a directory of its own on
its ``PATH``, and a package manager already pointed at it, so "I need `jq`" has an ordinary
ending. Nothing lands on the machine: the packages come out of a shared, read-only, content-
addressed store, and what the session owns is a directory of symlinks into it that is deleted
when the session is reaped.

Nix is what implements it, and its properties are the reason this is possible at all rather than
reckless. Installing does not write to the machine's own profile, needs no privilege the session
does not have, and cannot half-apply: the store is immutable, so the worst a session can do to
itself is add a name to a directory nobody else reads. On a machine without Nix there is no
toolbox and nothing is said about one — the agent is not told about a capability it does not
have.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from frank.base import environment_variables
from frank.base.paths import session_toolbox_directory, session_toolboxes_directory

# What the environment has to say for the ordinary command to mean "this session's own".
#
# `XDG_STATE_HOME` is where the package manager keeps a user's profile, and pointing it at the
# session's directory is what makes `nix profile add nixpkgs#jq` install *here* with no flag, no
# path and no variable for the agent to carry. The alternative spelling — handing the agent a
# variable of its own to remember and pass on every call — works and reads like a chore, which
# is exactly the kind of thing a model drops halfway through a long turn.
_XDG_STATE = environment_variables.XDG_STATE_HOME
_NIX_CONFIG = environment_variables.NIX_CONFIG
# Additive rather than replacing: `NIX_CONFIG` is layered over the machine's and the user's
# configuration files, so a private cache or substituter the person configured keeps working.
# `NIX_USER_CONF_FILES` would have replaced the user's file wholesale.
_XDG_SETTING = "use-xdg-base-directories = true"


@dataclass(frozen=True)
class Toolbox:
    """One session's own tools: where they live, and what a child needs in its environment to
    find them and add to them."""

    session_id: str
    root: Path

    @property
    def profile(self) -> Path:
        """The profile directory the package manager maintains for this session."""
        return self.root / "nix" / "profile"

    @property
    def binaries(self) -> Path:
        """What goes on ``PATH``. It does not exist until the session installs something, and a
        missing entry on ``PATH`` is harmless — which is why nothing here has to create it."""
        return self.profile / "bin"

    def environment(self, inherited: Optional[dict[str, str]] = None) -> dict[str, str]:
        """The environment a tool child needs to have this toolbox.

        ``PATH`` is prepended rather than replaced, so a session that installs its own `python`
        gets that one and everything the machine already provided still works."""
        base = dict(inherited if inherited is not None else os.environ)
        existing = base.get("PATH", "")
        return {
            "PATH": f"{self.binaries}:{existing}" if existing else str(self.binaries),
            _XDG_STATE: str(self.root),
            _NIX_CONFIG: _joined_config(base.get(_NIX_CONFIG, "")),
        }

    def prepare(self) -> "Toolbox":
        """Make sure the directory exists. Called when a session's tools are first wired up
        rather than when the session is created, so a session that never runs a command leaves
        nothing behind."""
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
    """Whether this machine can give a session tools of its own.

    Cached because it is asked once per session and answers a fact about the machine. A machine
    that gains Nix mid-run needs the daemon restarted to notice, which is true of every other
    thing the daemon probes once."""
    return shutil.which("nix") is not None


def toolbox_for(session_id: str, *, enabled: bool = True) -> Optional[Toolbox]:
    """This session's toolbox, or ``None`` when it has none.

    ``None`` is not a degraded toolbox and must not be treated as one: it is the answer for a
    machine without Nix and for a person who turned the whole thing off, and in both cases the
    agent is told nothing about installing anything."""
    if not enabled or not session_id or not available():
        return None
    return Toolbox(session_id=session_id, root=session_toolbox_directory(session_id))


def discard(session_id: str) -> None:
    """Delete a session's toolbox, because the session is over.

    The packages themselves are not deleted and are not this function's to delete — they live in
    the shared store, where the package manager's own garbage collection reclaims them once no
    profile refers to them. What goes is the directory of symlinks that was this session's."""
    if not session_id:
        return
    root = session_toolbox_directory(session_id)
    shutil.rmtree(root, ignore_errors=True)


def sweep(live_session_ids: Iterable[str]) -> list[str]:
    """Delete the toolboxes of sessions that are gone, and answer with what went.

    A session's toolbox is deleted when it is reaped, which covers every ordinary ending. It
    does not cover a daemon that was killed, a machine that lost power, or a session whose
    record went without its process being asked — and what is left behind then is a directory
    that nothing will ever look at again. Swept at startup, because that is the moment the
    daemon knows the full set of sessions that still exist, and the answer cannot be computed
    one session at a time."""
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
