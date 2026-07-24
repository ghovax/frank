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
import time
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
        return [sys.executable, "daemon"]
    return [sys.executable, "-m", "xeac", "daemon"]


def ensure_daemon() -> None:
    """Start the daemon if it is not already up, and wait until it answers."""
    if daemon_is_up():
        return
    try:
        subprocess.Popen(
            _daemon_command(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            # Detached, so the daemon outlives the command that started it — otherwise every
            # CLI invocation would take the fleet down with it on exit.
            start_new_session=True,
        )
    except OSError as error:
        raise DaemonError(f"Could not start xeacd: {error}") from error

    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if daemon_is_up():
            return
        time.sleep(0.1)
    raise DaemonError("xeacd did not become ready in time. Check the daemon log.")


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
