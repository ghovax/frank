"""Where Frank keeps things on disk, following the XDG Base Directory convention.

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
    """An XDG directory: the environment variable when it names an absolute path, else
    the convention's default. A relative value is ignored, as the specification requires."""
    raw = os.environ.get(variable, "").strip()
    base = Path(raw) if raw.startswith("/") else default
    path = base / APPLICATION
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_directory() -> Path:
    """User-editable configuration (``~/.config/frank``)."""
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config")


def data_directory() -> Path:
    """Durable state that must survive — databases, uploads, secrets, workspaces
    (``~/.local/share/frank``)."""
    return _xdg("XDG_DATA_HOME", Path.home() / ".local" / "share")


def state_directory() -> Path:
    """Logs and pidfiles (``~/.local/state/frank``)."""
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state")


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


def session_toolboxes_directory() -> Path:
    """Where every session's own tools live, one directory per session.

    Named in its own right rather than derived from a session's path, because the sweep that
    removes the toolboxes of sessions that are gone needs *this* directory and nothing above
    it. Taking it as the parent of a session's path is one empty string away from naming the
    state directory itself, which is not a mistake worth leaving available to make."""
    return state_directory() / "sessions"


def session_toolbox_directory(session_id: str) -> Path:
    """Where one session keeps the tools it installed for itself.

    Under state rather than runtime: it has to survive the session going to sleep between turns,
    which is what `XDG_RUNTIME_DIR` explicitly does not promise. Not created here — a session
    that never installs anything should leave nothing behind — see `frank.base.toolbox`."""
    return session_toolboxes_directory() / session_id


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
    """One provider's OAuth tokens (``…/frank/oauths/<provider>.json``).

    There is no reading of any older layout. A token file written where this used to put one
    is simply not found, and the provider reports itself signed out until the user signs in
    again — which costs one browser round trip and leaves exactly one place a token can be.
    Carrying a relocation would mean this function had to know the shape of every layout it
    ever had, forever, and a sign-out would have to delete files from all of them."""
    return oauths_directory() / f"{provider_identifier}.json"


# The `sockaddr_un.sun_path` limit, which is an operating-system constant rather than a
# filesystem one: 104 bytes on macOS and the BSDs, 108 on Linux. The smaller is used
# everywhere, so a path that binds on one platform binds on all of them — a limit that differs
# by platform is a bug that only appears on the platform you did not develop on, which is
# exactly how this one was found.
SOCKET_PATH_MAXIMUM_BYTES = 104


class SocketPathTooLong(OSError):
    """A unix socket path exceeds what `bind(2)` accepts.

    Its own error because the alternative is what happened: `bind` fails with a bare
    ``OSError: AF_UNIX path too long`` that names neither the path nor the limit, several
    frames below the code that built it. Every session on macOS failed this way and nothing
    reported which path was at fault.
    """


def _within_socket_limit(path: Path) -> Path:
    """The path, if it can actually be bound; otherwise a refusal that says why.

    Checked at construction rather than at bind, so the caller that chose the directory is the
    one that hears about it."""
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


# How many hex characters name an SSH control socket. The budget is what decides it: a macOS
# `$TMPDIR` runtime directory is ~63 bytes, and a unix socket path may be 104, so the name has
# about forty to spend. Sixteen leaves comfortable headroom and is far past the point where a
# collision between two host aliases on one machine is worth reasoning about.
SSH_CONTROL_IDENTIFIER_LENGTH = 16


def ssh_control_identifier(host_alias: str) -> str:
    """The filename for one host's multiplexed SSH control socket.

    A digest rather than ssh's own `%C`, and that is the fix rather than a preference. `%C` is a
    full SHA-1 — forty characters — so under a macOS `$TMPDIR` the ControlPath came to 111 bytes
    against a 104-byte limit and ssh refused every connection with `ControlPath too long`, which
    made every remote environment unreachable on the platform this ships for. The comment where
    it was used called `%C` "length-safe"; it is length-*stable*, which is not the same thing,
    and the directory it sits in is what decides whether it fits.

    Keyed on the alias because the alias is what a location connects by: one alias is one
    connection to share, and two aliases pointing at the same host simply get a socket each,
    which costs a connection and breaks nothing. `%C` additionally folds in the resolved host,
    port and user — a distinction that only matters for a config where one alias resolves two
    ways, which is not a thing a workspace environment can express."""
    return hashlib.sha256(host_alias.encode()).hexdigest()[:SSH_CONTROL_IDENTIFIER_LENGTH]


def ssh_control_directory() -> Path:
    """Where multiplexed SSH control sockets live, guaranteed short enough to bind.

    The runtime directory, like every other socket the harness owns — unless it is so deep that
    even a short name would not fit, in which case a per-user directory directly under the
    system temporary root is used instead. That fallback is not decoration: `XDG_RUNTIME_DIR`
    can be set to anything, and a remote environment failing to connect because of how somebody
    configured a directory is a bad way to find out about it."""
    preferred = runtime_directory() / "ssh"
    if len(str(preferred).encode()) + 1 + SSH_CONTROL_IDENTIFIER_LENGTH <= SOCKET_PATH_MAXIMUM_BYTES:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    # `/tmp` literally, not `tempfile.gettempdir()` — on macOS that *is* the long path this
    # is escaping from. Everything reached over SSH is POSIX by construction.
    fallback = Path("/tmp") / f"{APPLICATION}-{os.getuid()}-ssh"
    fallback.mkdir(parents=True, exist_ok=True)
    fallback.chmod(0o700)
    return fallback


def session_socket_identifier(session_id: str) -> str:
    """The short, stable filename stem for a session's socket.

    A session id is `session-` plus a full dashed UUID, which is 44 characters; under a macOS
    `$TMPDIR` — `/var/folders/xy/<22 chars>/T/` — the resulting socket path is 117 bytes
    against a 104-byte limit, so **no session could bind on macOS at all**. The id itself
    cannot shrink: it is the durable identity, it appears in the registry, in `frank ps` and in
    every log line.

    So the socket is named by a digest of the id instead. Still a pure function of it, which is
    what `lifecycle`'s unlink-on-reap depends on, and 16 hex characters is far past the point
    where a collision between concurrent sessions on one machine is worth reasoning about."""
    return hashlib.sha256(session_id.encode()).hexdigest()[:16]


def session_socket_path(session_id: str) -> Path:
    path = runtime_directory() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return _within_socket_limit(path / f"{session_socket_identifier(session_id)}.sock")


def prototype_socket_path() -> Path:
    """Where the daemon reaches the prototype.

    Beside the daemon's own socket in the runtime directory, and 0600 like it: anything that
    can speak this socket can make the prototype fork a process running arbitrary agent
    configuration, so it is exactly as sensitive as the daemon's control socket."""
    return _within_socket_limit(runtime_directory() / PROTOTYPE_SOCKET_FILENAME)


def daemon_token_path() -> Path:
    """The capability token the daemon mints at startup. Written 0600: it is what proves a
    client may drive the control plane, so file permissions are the access control."""
    return runtime_directory() / DAEMON_TOKEN_FILENAME


def daemon_port_path() -> Path:
    """The loopback port the daemon listens on for GUI clients, which cannot open a unix
    socket. Written beside the token so a client discovers both together."""
    return runtime_directory() / DAEMON_PORT_FILENAME


def reach_token_path() -> Path:
    """The token a phone presents to `frank reach`. Written 0600, like the daemon's.

    In the data directory rather than the runtime one, which is the whole difference between
    this token and the daemon's: the daemon mints a fresh capability token every boot, and a
    device paired against that would be unpaired by a restart. This one is minted once and
    kept, so an endpoint stays an endpoint. Rotating it is therefore an act, not a side effect
    of rebooting — `frank reach rotate`."""
    return data_directory() / "reach-token"


def log_file_path(name: str) -> Path:
    return state_directory() / f"{name}.log"
