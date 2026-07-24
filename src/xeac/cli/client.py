"""Talking to the daemon, and starting it if it is not there.

The CLI finds the daemon the way anything else does: a socket, a port, and a token in the
runtime directory. If none of that is there it starts the daemon and waits — the same
autostart a container runtime does, so the first command a user types works without a
separate "start the service" step.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any, Optional

import httpx

from xeac.base.paths import daemon_port_path, daemon_socket_path, daemon_token_path

# How long to wait for a freshly started daemon to publish its handshake. Generous, because a
# frozen build pays a real import cost on first launch.
_STARTUP_TIMEOUT_SECONDS = 45.0


class DaemonError(RuntimeError):
    """The daemon could not be reached, or refused the call."""


def _read_token() -> str:
    try:
        return daemon_token_path().read_text().strip()
    except OSError:
        return ""


def daemon_is_up() -> bool:
    """Whether something is actually listening, rather than whether a socket file exists.

    A daemon that was killed leaves its socket behind, so existence proves nothing; only a
    connection does."""
    path = daemon_socket_path()
    if not path.exists() or not _read_token():
        return False
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(path)), timeout=2.0) as client:
            return client.get("http://daemon/health").status_code == 200
    except (httpx.HTTPError, OSError):
        return False


def _daemon_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "xeacd"]
    return [sys.executable, "-m", "xeac", "xeacd"]


def ensure_daemon() -> None:
    """Start the daemon if it is not already up, and wait until it answers.

    Readiness is read from the daemon itself: it writes one line to stdout the moment both of
    its listeners are accepting connections, and closes the stream. Waiting on that line means
    the first command after an autostart proceeds exactly when the daemon is usable, rather
    than at whatever interval a poll happened to choose."""
    if daemon_is_up():
        return
    try:
        daemon = subprocess.Popen(
            _daemon_command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
            # Detached, so the daemon outlives the command that started it — otherwise every
            # CLI invocation would take the fleet down with it on exit.
            start_new_session=True,
        )
    except OSError as error:
        raise DaemonError(f"Could not start xeacd: {error}") from error

    announcement = _await_announcement(daemon)
    if announcement is None:
        raise DaemonError(
            "xeacd exited before it was ready. Check the daemon log under the state directory."
            if daemon.poll() is not None
            else "xeacd did not become ready in time. Check the daemon log under the state directory."
        )


def _await_announcement(daemon: subprocess.Popen) -> Optional[dict]:
    """The daemon's one-line readiness announcement, or ``None`` if it never arrives.

    The read runs on a worker thread so the timeout is real: a blocking `readline` on a daemon
    that wedged before announcing would otherwise hang the command with no way out."""
    if daemon.stdout is None:
        return None
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(daemon.stdout.readline)
        try:
            line = pending.result(timeout=_STARTUP_TIMEOUT_SECONDS)
        except FuturesTimeout:
            return None
    if not line:
        return None
    try:
        announcement = json.loads(line)
    except ValueError:
        return None
    return announcement if announcement.get("ready") else None


def call(method: str, **params: Any) -> dict:
    """One control-plane call, autostarting the daemon if needed."""
    ensure_daemon()
    token = _read_token()
    try:
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(daemon_socket_path())),
            timeout=300.0,
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            response = client.post("http://daemon/rpc", json={"method": method, "params": params})
    except (httpx.HTTPError, OSError) as error:
        raise DaemonError(f"Could not reach xeacd: {error}") from error

    try:
        body = response.json()
    except ValueError as error:
        raise DaemonError(f"xeacd returned something that was not JSON ({response.status_code}).") from error
    if "error" in body:
        raise DaemonError(body["error"].get("message") or "The call failed.")
    if response.status_code >= 400:
        raise DaemonError(f"xeacd rejected {method} ({response.status_code}).")
    return body.get("result", {})


def stream(path: str):
    """Follow one of the daemon's event streams, yielding decoded frames."""
    ensure_daemon()
    token = _read_token()
    with httpx.Client(
        transport=httpx.HTTPTransport(uds=str(daemon_socket_path())),
        timeout=None,
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        with client.stream("GET", f"http://daemon{path}") as response:
            buffer = ""
            for chunk in response.iter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    frame, buffer = buffer.split("\n\n", 1)
                    for line in frame.splitlines():
                        if line.startswith("data:"):
                            try:
                                yield json.loads(line[5:].strip())
                            except ValueError:
                                continue
