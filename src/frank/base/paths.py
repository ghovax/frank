"""Where Frank keeps things on disk, following the XDG Base Directory convention."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

APPLICATION = "frank"

CONFIGURATION_FILENAME = "configuration.yaml"
DATABASE_FILENAME = "history.db"
BACKGROUND_DATABASE_FILENAME = "background.db"
DAEMON_SOCKET_FILENAME = "frankd.sock"
PROTOTYPE_SOCKET_FILENAME = "prototype.sock"
DAEMON_TOKEN_FILENAME = "token"
DAEMON_PORT_FILENAME = "port"


def _xdg(variable: str, default: Path) -> Path:
    """An XDG directory: the environment variable when it names an absolute path, else the convention's default."""
    raw = os.environ.get(variable, "").strip()
    base = Path(raw) if raw.startswith("/") else default
    path = base / APPLICATION
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_directory() -> Path:
    """User-editable configuration (``~/.config/frank``)."""
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config")


def data_directory() -> Path:
    """Durable state that must survive — databases, uploads, secrets, workspaces (``~/.local/share/frank``)."""
    return _xdg("XDG_DATA_HOME", Path.home() / ".local" / "share")


def state_directory() -> Path:
    """Logs and pidfiles (``~/.local/state/frank``)."""
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state")


def runtime_directory() -> Path:
    """Sockets and the daemon's handshake files."""
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


def session_toolboxes_directory() -> Path:
    """Where every session's own tools live, one directory per session."""
    return state_directory() / "sessions"


def session_toolbox_directory(session_id: str) -> Path:
    """Where one session keeps the tools it installed for itself."""
    return session_toolboxes_directory() / session_id


def workspaces_directory() -> Path:
    path = data_directory() / "workspaces"
    path.mkdir(parents=True, exist_ok=True)
    return path


def oauths_directory() -> Path:
    """The OAuth token files, one per provider that signs in rather than taking a key."""
    path = data_directory() / "oauths"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def oauth_token_path(provider_identifier: str) -> Path:
    """One provider's OAuth tokens (``…/frank/oauths/<provider>.json``)."""
    return oauths_directory() / f"{provider_identifier}.json"


# The `sockaddr_un.sun_path` limit, which is an operating-system constant rather than a filesystem one: 104 bytes on macOS and the BSDs, 108 on Linux.
SOCKET_PATH_MAXIMUM_BYTES = 104


class SocketPathTooLong(OSError):
    """A unix socket path exceeds what `bind(2)` accepts."""


def _within_socket_limit(path: Path) -> Path:
    """The path, if it can actually be bound; otherwise a refusal that says why."""
    encoded = len(str(path).encode())
    if encoded > SOCKET_PATH_MAXIMUM_BYTES:
        raise SocketPathTooLong(
            f"{path} is {encoded} bytes, and a unix socket path may be at most "
            f"{SOCKET_PATH_MAXIMUM_BYTES}. The runtime directory is too deep — set "
            f"XDG_RUNTIME_DIR to something shorter."
        )
    return path


def daemon_socket_path() -> Path:
    return _within_socket_limit(runtime_directory() / DAEMON_SOCKET_FILENAME)


# How many hex characters name an SSH control socket.
SSH_CONTROL_IDENTIFIER_LENGTH = 16


def ssh_control_identifier(host_alias: str) -> str:
    """The filename for one host's multiplexed SSH control socket."""
    return hashlib.sha256(host_alias.encode()).hexdigest()[:SSH_CONTROL_IDENTIFIER_LENGTH]


def ssh_control_directory() -> Path:
    """Where multiplexed SSH control sockets live, guaranteed short enough to bind."""
    preferred = runtime_directory() / "ssh"
    if len(str(preferred).encode()) + 1 + SSH_CONTROL_IDENTIFIER_LENGTH <= SOCKET_PATH_MAXIMUM_BYTES:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    # `/tmp` literally, not `tempfile.gettempdir()` — on macOS that *is* the long path this is escaping from.
    fallback = Path("/tmp") / f"{APPLICATION}-{os.getuid()}-ssh"
    fallback.mkdir(parents=True, exist_ok=True)
    fallback.chmod(0o700)
    return fallback


def session_socket_identifier(session_id: str) -> str:
    """The short, stable filename stem for a session's socket."""
    return hashlib.sha256(session_id.encode()).hexdigest()[:16]


def session_socket_path(session_id: str) -> Path:
    path = runtime_directory() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return _within_socket_limit(path / f"{session_socket_identifier(session_id)}.sock")


def prototype_socket_path() -> Path:
    """Where the daemon reaches the prototype."""
    return _within_socket_limit(runtime_directory() / PROTOTYPE_SOCKET_FILENAME)


def daemon_token_path() -> Path:
    """The capability token the daemon mints at startup."""
    return runtime_directory() / DAEMON_TOKEN_FILENAME


def daemon_port_path() -> Path:
    """The loopback port the daemon listens on for GUI clients, which cannot open a unix socket."""
    return runtime_directory() / DAEMON_PORT_FILENAME


def reach_token_path() -> Path:
    """The token a phone presents to `frank reach`. Written 0600, like the daemon's."""
    return data_directory() / "reach-token"


def log_file_path(name: str) -> Path:
    return state_directory() / f"{name}.log"
