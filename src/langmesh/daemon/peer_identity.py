"""Who is on the other end of the unix socket, according to the kernel rather than a presented token."""

from __future__ import annotations

import logging
import os
import socket
import struct
import subprocess
import sys
from typing import Optional

logger = logging.getLogger(__name__)

# Linux hands back (pid, uid, gid); macOS exposes the peer's pid on its own option under SOL_LOCAL.
_SOL_LOCAL = 0
_LOCAL_PEERPID = 2

# What `scope["client"]` carries for a unix socket, in place of a TCP connection's host and port.
UNIX_PEER = "unix"


def peer_process_id(transport) -> int:
    """The pid of the process on the other end, or 0 when the platform cannot say."""
    raw_socket = transport.get_extra_info("socket")
    if raw_socket is None:
        return 0
    try:
        if hasattr(socket, "SO_PEERCRED"):
            credentials = raw_socket.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            return int(struct.unpack("3i", credentials)[0])
        if sys.platform == "darwin":
            return int(struct.unpack("i", raw_socket.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, 4))[0])
    except (OSError, struct.error):
        return 0
    return 0


def session_for_process(process_id: int) -> Optional[str]:
    """The session a process belongs to, by the group its tool child leads, which the daemon recorded when it spawned it."""
    from langmesh.daemon import state

    if process_id <= 0 or state.host is None:
        return None
    for resolve in (os.getpgid, os.getsid):
        try:
            owner = state.host.session_of_group(resolve(process_id))
        except (OSError, ProcessLookupError):
            return None
        if owner:
            return owner
    return None


def _all_process_ids() -> list[int]:
    """Every pid on this machine. Only the list comes from outside, because only the list is portable."""
    try:
        return [int(entry) for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError:
        pass
    try:
        listing = subprocess.run(
            ["ps", "-A", "-o", "pid="], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return []
    process_ids = []
    for line in listing.stdout.split():
        try:
            process_ids.append(int(line))
        except ValueError:
            continue
    return process_ids


def session_process_groups(leader_pid: int) -> list[int]:
    """Every process group inside one session's process session, the leader's included."""
    own_group = os.getpgid(0)
    groups: dict[int, None] = {}
    for process_id in _all_process_ids():
        try:
            if os.getsid(process_id) != leader_pid:
                continue
            group = os.getpgid(process_id)
        except (OSError, ProcessLookupError):
            continue  # exited between the listing and the question
        if group != own_group:
            groups.setdefault(group, None)
    return list(groups)


def unix_peer_protocol():
    """uvicorn's HTTP protocol taught to record who connected, since credentials exist only while it does."""
    from uvicorn.protocols.http.auto import AutoHTTPProtocol

    class PeerAwareProtocol(AutoHTTPProtocol):  # type: ignore[misc, valid-type]
        def connection_made(self, transport):  # noqa: ANN001 — matches asyncio's signature
            super().connection_made(transport)
            process_id = peer_process_id(transport)
            if process_id:
                # Stands in for the address a TCP peer would have, read back out of the scope by every request.
                self.client = (UNIX_PEER, process_id)

    return PeerAwareProtocol


def calling_session(scope) -> Optional[str]:
    """The session that made this request, when the kernel identified it as one."""
    client = scope.get("client")
    if not client or len(client) != 2 or client[0] != UNIX_PEER:
        return None
    return session_for_process(int(client[1]))


__all__ = [
    "UNIX_PEER",
    "calling_session",
    "peer_process_id",
    "session_for_process",
    "session_process_groups",
    "unix_peer_protocol",
]
