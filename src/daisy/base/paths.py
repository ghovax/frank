"""Where Daisy keeps things on disk, following the XDG Base Directory convention.

A single dot-directory is the un-unixy choice, so state is split by what it *is*:
configuration the user edits, durable data that must survive, runtime sockets that must
not, regenerable caches, and logs. Every location honours its ``XDG_*`` environment
variable and falls back to the conventional default, on every platform — the CLI is the
primary surface, so it behaves the way a terminal user expects even on macOS.

Sockets live under ``XDG_RUNTIME_DIR`` deliberately: it is a per-user directory the
operating system clears on logout, so a stale socket from a crashed daemon disappears on
its own rather than becoming something this code has to reap.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

APPLICATION = "daisy"

CONFIGURATION_FILENAME = "configuration.yaml"
DATABASE_FILENAME = "history.db"
BACKGROUND_DATABASE_FILENAME = "background.db"
DAEMON_SOCKET_FILENAME = "daisyd.sock"
DAEMON_TOKEN_FILENAME = "token"
DAEMON_PORT_FILENAME = "port"


def _xdg(variable: str, default: Path) -> Path:
    """An XDG directory: the environment variable when it names an absolute path, else
    the convention's default. A relative value is ignored, as the specification requires."""
    raw = os.environ.get(variable, "").strip()
    base = Path(raw) if raw.startswith("/") else default
    path = base / APPLICATION
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_directory() -> Path:
    """User-editable configuration (``~/.config/daisy``)."""
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config")


def data_directory() -> Path:
    """Durable state that must survive — databases, uploads, secrets, workspaces
    (``~/.local/share/daisy``)."""
    return _xdg("XDG_DATA_HOME", Path.home() / ".local" / "share")


def state_directory() -> Path:
    """Logs and pidfiles (``~/.local/state/daisy``)."""
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state")


def cache_directory() -> Path:
    """Regenerable caches, safe to delete at any time (``~/.cache/daisy``)."""
    return _xdg("XDG_CACHE_HOME", Path.home() / ".cache")


def runtime_directory() -> Path:
    """Sockets and the daemon's handshake files.

    ``XDG_RUNTIME_DIR`` is the correct home for these because the OS clears it on logout.
    It is not always set (notably on macOS), so the fallback is a per-user directory under
    the system temporary directory, created 0700 so another user on the machine cannot
    reach a session's socket."""
    raw = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if raw.startswith("/"):
        path = Path(raw) / APPLICATION
    else:
        path = Path(tempfile.gettempdir()) / f"{APPLICATION}-{os.getuid()}"
    path.mkdir(parents=True, exist_ok=True)
    # The socket directory is the security boundary for every session's endpoint.
    path.chmod(0o700)
    return path


def configuration_file_path() -> Path:
    return config_directory() / CONFIGURATION_FILENAME


def database_file_path() -> Path:
    return data_directory() / DATABASE_FILENAME


def background_database_path() -> Path:
    return data_directory() / BACKGROUND_DATABASE_FILENAME


def uploads_directory() -> Path:
    path = data_directory() / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def workspaces_directory() -> Path:
    path = data_directory() / "workspaces"
    path.mkdir(parents=True, exist_ok=True)
    return path


def oauths_directory() -> Path:
    """The OAuth token files, one per provider that signs in rather than taking a key.

    Created 0700, unlike the other data subdirectories, because it holds nothing but
    password-equivalent secrets. The token files are written 0600 themselves; the mode here
    is so a file added to this directory later is protected by where it lives rather than by
    whoever remembers to chmod it."""
    path = data_directory() / "oauths"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def oauth_token_path(provider_identifier: str) -> Path:
    """One provider's OAuth tokens (``…/daisy/oauths/<provider>.json``).

    There is no reading of any older layout. A token file written where this used to put one
    is simply not found, and the provider reports itself signed out until the user signs in
    again — which costs one browser round trip and leaves exactly one place a token can be.
    Carrying a relocation would mean this function had to know the shape of every layout it
    ever had, forever, and a sign-out would have to delete files from all of them."""
    return oauths_directory() / f"{provider_identifier}.json"


def daemon_socket_path() -> Path:
    return runtime_directory() / DAEMON_SOCKET_FILENAME


def session_socket_path(session_id: str) -> Path:
    path = runtime_directory() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{session_id}.sock"


def daemon_token_path() -> Path:
    """The capability token the daemon mints at startup. Written 0600: it is what proves a
    client may drive the control plane, so file permissions are the access control."""
    return runtime_directory() / DAEMON_TOKEN_FILENAME


def daemon_port_path() -> Path:
    """The loopback port the daemon listens on for GUI clients, which cannot open a unix
    socket. Written beside the token so a client discovers both together."""
    return runtime_directory() / DAEMON_PORT_FILENAME


def log_file_path(name: str) -> Path:
    return state_directory() / f"{name}.log"
