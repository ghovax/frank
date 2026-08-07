"""Resolve a location record into its model-facing URI and an executor to run tools against it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import location_uri, ssh_hosts
from .executor import LocalExecutor, LocationExecutor, SshExecutor


@dataclass(frozen=True)
class LocationAddress:
    """The connection-relevant fields of a location, independent of storage."""

    kind: str  # "local" | "remote"
    base_directory: str
    host_alias: str = ""


def location_uri_for(address: LocationAddress) -> str:
    """The URI the agent passes as ``location``, derived for a remote from the resolved host."""
    if address.kind == "local":
        return location_uri.format_local(address.base_directory)
    if address.kind == "remote":
        if not address.host_alias:
            raise ValueError("A remote location requires an ssh host alias.")
        host = ssh_hosts.resolve_host(address.host_alias)
        if host is None:
            return location_uri.format_remote(address.host_alias, address.base_directory)
        return location_uri.format_remote(
            host.hostname, address.base_directory, user=host.user, port=host.port
        )
    raise ValueError(f"Unknown location kind: {address.kind!r}")


def host_is_defined(alias: str) -> bool:
    """Whether an ssh alias is declared in ~/.ssh/config, so a location naming a dead host can be flagged."""
    return any(host.alias == alias for host in ssh_hosts.list_ssh_hosts())


def executor_for(
    address: LocationAddress, *, control_directory: Path | None = None
) -> LocationExecutor:
    """The executor that runs tools against this location."""
    if address.kind == "local":
        return LocalExecutor()
    if address.kind == "remote":
        if not address.host_alias:
            raise ValueError("A remote location requires an ssh host alias.")
        return SshExecutor(address.host_alias, control_directory=control_directory)
    raise ValueError(f"Unknown location kind: {address.kind!r}")
