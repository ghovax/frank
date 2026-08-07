"""Talking to the daemon, and starting it if it is not there."""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Any, Optional

import httpx

from langmesh.base.paths import daemon_socket_path, daemon_token_path
from langmesh.base.tuning import Tunable, active_tuning


class DaemonError(RuntimeError):
    """The daemon could not be reached, or refused the call."""


def _read_token() -> str:
    try:
        return daemon_token_path().read_text().strip()
    except OSError:
        return ""


def daemon_is_up() -> bool:
    """Whether something is actually listening, since a killed daemon leaves its socket behind."""
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
        return [sys.executable, "langmeshd"]
    return [sys.executable, "-m", "langmesh", "langmeshd"]


def ensure_daemon() -> None:
    """Start the daemon if it is not up, and wait for the readiness line it writes itself."""
    if daemon_is_up():
        return
    try:
        daemon = subprocess.Popen(
            _daemon_command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
            # Detached, so the daemon outlives the command that started it.
            start_new_session=True,
        )
    except OSError as error:
        raise DaemonError(f"Could not start langmeshd: {error}") from error

    announcement = _await_announcement(daemon)
    if announcement is None:
        raise DaemonError(
            "langmeshd exited before it was ready. Check the daemon log under the state directory."
            if daemon.poll() is not None
            else "langmeshd did not become ready in time. Check the daemon log under the state directory."
        )


def _await_announcement(daemon: subprocess.Popen) -> Optional[dict]:
    """The daemon's readiness announcement, or `None`, read on a worker thread so the timeout is real."""
    if daemon.stdout is None:
        return None
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(daemon.stdout.readline)
        try:
            line = pending.result(timeout=active_tuning().duration(Tunable.daemon_startup))
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
        raise DaemonError(f"Could not reach langmeshd: {error}") from error

    try:
        body = response.json()
    except ValueError as error:
        raise DaemonError(
            f"langmeshd returned something that was not JSON ({response.status_code})."
        ) from error
    if "error" in body:
        raise DaemonError(body["error"].get("message") or "The call failed.")
    if response.status_code >= 400:
        raise DaemonError(f"langmeshd rejected {method} ({response.status_code}).")
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
            if response.status_code >= 400:
                # A refused stream still parses as no frames, so without this an attach ends instantly and silently.
                response.read()
                try:
                    message = response.json()["error"]["message"]
                except (ValueError, KeyError, TypeError):
                    message = f"langmeshd refused the stream ({response.status_code})."
                raise DaemonError(message)
            buffer = ""
            for chunk in response.iter_text():
                # Frames are separated by a blank line whose wire form is a carriage return and newline.
                buffer += chunk.replace("\r\n", "\n")
                while "\n\n" in buffer:
                    frame, buffer = buffer.split("\n\n", 1)
                    for line in frame.splitlines():
                        if line.startswith("data:"):
                            try:
                                yield json.loads(line[5:].strip())
                            except ValueError:
                                continue
