"""Fully-qualified location URIs, so the identifier the model passes is unambiguous on its own."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

LOCAL_SCHEME = "file"
REMOTE_SCHEME = "ssh"


@dataclass(frozen=True)
class LocationTarget:
    """The coordinates a location URI encodes, with the absolute directory tools treat as their cwd."""

    kind: str  # "local" | "remote"
    base_directory: str
    user: str = ""
    host: str = ""
    port: int = 22

    @property
    def is_remote(self) -> bool:
        return self.kind == "remote"


def _normalize_base_directory(base_directory: str) -> str:
    base_directory = (base_directory or "").strip()
    if not base_directory:
        raise ValueError("A location base_directory is required.")
    if not base_directory.startswith("/"):
        raise ValueError(f"A location base_directory must be absolute, got: {base_directory!r}")
    # Collapse a trailing slash (except the filesystem root) so URIs are canonical.
    return base_directory.rstrip("/") or "/"


def format_local(base_directory: str) -> str:
    """The URI for a location on the home server's own filesystem."""
    path = _normalize_base_directory(base_directory)
    return f"{LOCAL_SCHEME}://{quote(path)}"


def format_remote(host: str, base_directory: str, user: str = "", port: int = 22) -> str:
    """The URI for a location reached over SSH. ``host`` is the resolved hostname."""
    host = (host or "").strip()
    if not host:
        raise ValueError("A remote location requires a host.")
    path = _normalize_base_directory(base_directory)
    authority = f"{quote(user)}@{host}" if user else host
    if port and int(port) != 22:
        authority = f"{authority}:{int(port)}"
    return f"{REMOTE_SCHEME}://{authority}{quote(path)}"


def parse(uri: str) -> LocationTarget:
    """Parse a location URI back into its coordinates, raising on anything malformed."""
    parts = urlsplit((uri or "").strip())
    if parts.scheme == LOCAL_SCHEME:
        if parts.netloc:
            raise ValueError(f"A local location URI must have an empty host: {uri!r}")
        return LocationTarget(
            kind="local", base_directory=_normalize_base_directory(unquote(parts.path))
        )
    if parts.scheme == REMOTE_SCHEME:
        if not parts.hostname:
            raise ValueError(f"A remote location URI requires a host: {uri!r}")
        return LocationTarget(
            kind="remote",
            base_directory=_normalize_base_directory(unquote(parts.path)),
            user=unquote(parts.username or ""),
            host=parts.hostname,
            port=parts.port or 22,
        )
    raise ValueError(f"Unrecognized location URI scheme in: {uri!r}")
