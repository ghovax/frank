"""Who is on the other end of the unix socket, according to the kernel.

Attribution used to rest on which token a caller presented, and that could not hold. The
daemon's token is a 0600 file in the runtime directory, and a session's own `bash` tool runs
as the same user — so a session could read it, call the control plane as an anonymous client,
and be handed a peer with no parent and no permission clamp. Every structural guarantee in
the harness hangs off that attribution: which sessions are yours, whose subtree you may
touch, and how loose a child may be.

A secret a process can read is not an identity. The kernel already knows which process opened
a socket, and it will not lie about it, so that is what identifies a caller here. `SO_PEERCRED`
on Linux and `LOCAL_PEERPID` on macOS give the peer's pid at accept time; every worker is made
a session leader when it is forked (``os.setsid()`` in :mod:`frank.worker.prototype`),
so ``getsid`` on that pid names the worker that owns it — directly for the worker itself, and
for every shell command, pipeline and `frank` invocation underneath it, which all inherit the
session id.

That inheritance is a constraint on the rest of the worker, not a free property of it. The
bash tool puts each command in its own process *group* so `killpg` can reap its subtree, and
it must stop there: a `setsid` would have bought the same reaping and cost the attribution.

The one way out is for a caller to `setsid` itself, which detaches it from the session's
process session entirely — as an MCP stdio server does, since the client library spawns it
detached, so a server that shells out to `frank` calls as nobody. It is worth being plain about
the shape of that: a process outside the session's process session is also outside what
`frank kill` signals and outside the tree the reaper walks. It has stopped being the session
rather than escaped as it.

The same fact is read in both directions here. :func:`session_for_process` answers "which session
is this caller", for attribution on the socket; :func:`session_process_groups` answers "what
belongs to this session", for reaping it. Keeping them together is deliberate — they must agree
about what membership means, and they only do so as long as both ask the kernel.
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import subprocess
import sys
from typing import Optional

logger = logging.getLogger(__name__)

# Linux hands back (pid, uid, gid) as three ints; macOS has no SO_PEERCRED and exposes the peer's pid on its own option under SOL_LOCAL.
_SOL_LOCAL = 0
_LOCAL_PEERPID = 2

# What `scope["client"]` carries for a unix-socket request, in place of the (host, port) a TCP connection would have. uvicorn only formats this for its access log, which is off.
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
    """The session a process belongs to, or ``None`` if it belongs to none.

    A worker is a session leader, so its descendants all report its pid as their session id —
    which is what makes this hold for a command the model ran rather than only for the worker
    itself."""
    from frank.daemon import state

    if process_id <= 0 or state.registry is None:
        return None
    try:
        leader = os.getsid(process_id)
    except (OSError, ProcessLookupError):
        return None
    for record in state.registry.live():
        if record.pid and record.pid == leader:
            return record.id
    return None


def _all_process_ids() -> list[int]:
    """Every pid on this machine.

    Only the *list* comes from outside, because only the list is portable: `ps -A -o pid=` is
    POSIX, while the session id is not — Linux spells it `sid`, and BSD `ps` prints `sess` as a
    kernel pointer rather than a pid. `os.getsid` supplies the part that has to be right. On Linux
    `/proc` answers the same question without spawning anything, so it is tried first."""
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
    """Every process group inside one session's process session, the leader's own included.

    This is the other direction of the same fact :func:`session_for_process` reads. A worker is a
    process-session leader, so `getsid` on any of its descendants returns its pid — which is what
    identifies a caller on the socket, and equally what identifies everything a session left
    running. The group is the wrong unit for that: the bash tool puts each command in a group of
    its own so one job can be cancelled with its subtree, and that same property puts it outside
    the group a session-wide signal would reach.

    The daemon's own group is excluded on principle rather than necessity — a worker is spawned
    into its own session, so the daemon is never in it — because a sweep that signals process
    groups should not depend on that staying true."""
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
    """uvicorn's HTTP protocol, taught to record who connected.

    Subclassed rather than reached for later because the peer's credentials are only available
    while the connection exists, and by the time a request is dispatched the ASGI scope has
    kept nothing about it — for a unix socket `scope["client"]` is simply ``None``."""
    from uvicorn.protocols.http.auto import AutoHTTPProtocol

    class PeerAwareProtocol(AutoHTTPProtocol):  # type: ignore[misc, valid-type]
        def connection_made(self, transport):  # noqa: ANN001 — matches asyncio's signature
            super().connection_made(transport)
            process_id = peer_process_id(transport)
            if process_id:
                # Stands in for the (host, port) a TCP peer would have.
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
