"""The SSH host registry, sourced from ``~/.ssh/config`` — the OS-level source of truth."""

from __future__ import annotations

import glob
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SSH_CONFIG_PATH = Path("~/.ssh/config").expanduser()
_PATTERN_CHARACTERS = set("*?!")


@dataclass(frozen=True)
class SshHost:
    """A connectable host from the SSH config: its ``alias`` (what we ssh to, so config fidelity is preserved) plus the resolved coordinates ``ssh -G`` reported."""

    alias: str
    hostname: str
    user: str = ""
    port: int = 22
    identity_files: tuple[str, ...] = field(default_factory=tuple)


def _config_paths(config_path: Path, _seen: set[Path] | None = None) -> list[Path]:
    """The config file plus every file it pulls in via ``Include`` (glob-expanded, relative paths resolved against the including file's directory, then ~/.ssh)."""
    seen = _seen if _seen is not None else set()
    resolved = config_path.expanduser()
    if resolved in seen or not resolved.is_file():
        return []
    seen.add(resolved)
    paths = [resolved]
    try:
        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return paths
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        keyword, _, remainder = stripped.partition(" ")
        if keyword.lower() != "include" or not remainder.strip():
            continue
        for token in remainder.split():
            candidate = Path(token).expanduser()
            bases = [resolved.parent] if not candidate.is_absolute() else [Path("/")]
            if not candidate.is_absolute():
                bases.append(DEFAULT_SSH_CONFIG_PATH.parent)
            for base in bases:
                for match in sorted(glob.glob(str((base / token).expanduser()))):
                    paths.extend(_config_paths(Path(match), seen))
    return paths


def _literal_aliases(config_path: Path) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    for path in _config_paths(config_path):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            keyword, _, remainder = stripped.partition(" ")
            if keyword.lower() != "host":
                continue
            for token in remainder.split():
                if token and not (_PATTERN_CHARACTERS & set(token)) and token not in seen:
                    seen.add(token)
                    aliases.append(token)
    return aliases


def resolve_host(alias: str, *, timeout: float = 5.0) -> SshHost | None:
    """Resolve one alias via ``ssh -G``."""
    try:
        completed = subprocess.run(
            ["ssh", "-G", alias],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    hostname = alias
    user = ""
    port = 22
    identity_files: list[str] = []
    for line in completed.stdout.splitlines():
        key, _, value = line.strip().partition(" ")
        key = key.lower()
        value = value.strip()
        if key == "hostname" and value:
            hostname = value
        elif key == "user" and value:
            user = value
        elif key == "port" and value.isdigit():
            port = int(value)
        elif key == "identityfile" and value:
            identity_files.append(os.path.expanduser(value))
    return SshHost(alias=alias, hostname=hostname, user=user, port=port, identity_files=tuple(identity_files))


def list_ssh_hosts(config_path: Path = DEFAULT_SSH_CONFIG_PATH) -> list[SshHost]:
    """Every connectable host defined in the SSH config, resolved. Missing config gives []."""
    hosts: list[SshHost] = []
    for alias in _literal_aliases(config_path):
        host = resolve_host(alias)
        if host is not None:
            hosts.append(host)
    return hosts
