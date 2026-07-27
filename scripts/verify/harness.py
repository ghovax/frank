"""What every stage needs: an isolated Daisy, a daemon to talk to, and a way to report.

Isolation is the whole reason this file exists. A stage that ran against the developer's real
XDG directories would read their sessions, write their history database and leave their daemon
in a state they did not ask for; every stage here gets a fresh set of temporary roots and a
daemon of its own, and takes them away afterwards.

Reporting is structured because the output is evidence. A stage says which claim it checked and
what it observed, one JSON object per line, so a failure can be read without re-running it
under a debugger and so a person can paste the result somewhere useful.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger("verify")

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE_ROOT = REPOSITORY_ROOT / "src"

# The prototype imports the whole runtime before it binds, which is the one cold start the
# architecture still has. Everything else here is fast.
PROTOTYPE_READY_SECONDS = 240.0
DAEMON_READY_SECONDS = 180.0


def report(event: str, **fields: Any) -> None:
    """One structured line of evidence."""
    logger.info(json.dumps({"event": event, **fields}, default=str))


@dataclass
class Outcome:
    """What a stage concluded, and what it saw."""

    name: str
    passed: bool
    skipped: bool = False
    reason: str = ""
    observations: dict[str, Any] = field(default_factory=dict)


class RpcError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:600]}")
        self.status = status
        self.body = body


class Daemon:
    """A daemon with its own directories, and the calls a stage makes against it."""

    def __init__(self, roots: dict[str, str], process: subprocess.Popen, port: str, token: str) -> None:
        self.roots = roots
        self.process = process
        self.port = port
        self.token = token

    def call(self, method: str, params: Optional[dict] = None, *, token: str = "", timeout: float = 120.0) -> Any:
        """One control-plane call. Raises on an error response rather than returning it, so a
        stage's own assertions stay about behaviour rather than about error handling."""
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/rpc",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token or self.token}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise RpcError(error.code, error.read().decode()) from error
        if "error" in payload:
            raise RpcError(400, json.dumps(payload["error"]))
        return payload.get("result")

    def try_call(self, method: str, params: Optional[dict] = None, *, token: str = "") -> tuple[Any, Optional[RpcError]]:
        """`call`, for the stages that are checking that something is *refused*."""
        try:
            return self.call(method, params, token=token), None
        except RpcError as error:
            return None, error

    def await_prototype(self, timeout: float = PROTOTYPE_READY_SECONDS) -> dict:
        """Wait for the prototype to finish importing the runtime and attach."""
        deadline = time.monotonic() + timeout
        status: dict = {}
        while time.monotonic() < deadline:
            status = (self.call("daemon.status") or {}).get("prototype", {})
            if status.get("alive"):
                return status
            time.sleep(0.5)
        return status

    def stop(self, *, signal_number: int = signal.SIGTERM) -> None:
        if self.process.poll() is not None:
            return
        self.process.send_signal(signal_number)
        try:
            self.process.wait(timeout=90)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=30)

    def logs(self) -> str:
        if self.process.stdout is None:
            return ""
        try:
            return self.process.stdout.read() or ""
        except Exception:  # noqa: BLE001
            return ""


def make_roots() -> dict[str, str]:
    """A fresh set of XDG directories, so a stage touches nothing of the developer's."""
    return {
        key: tempfile.mkdtemp(prefix=f"daisy-verify-{key.lower()}-")
        for key in ("XDG_RUNTIME_DIR", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME")
    }


def seed_configuration(roots: dict[str, str], text: str) -> Path:
    """Write a configuration file into an isolated root, and answer with its path."""
    path = Path(roots["XDG_CONFIG_HOME"]) / "daisy" / "configuration.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def environment_for(roots: dict[str, str]) -> dict[str, str]:
    return {**os.environ, **roots, "PYTHONPATH": str(SOURCE_ROOT)}


def confinement_available() -> tuple[bool, str]:
    """Whether this machine can actually enforce a confinement profile.

    Asked by running the real probe rather than by guessing from the platform: `sandbox-exec`
    being on disk and Apple still honouring it are different questions, and Landlock needs a
    kernel new enough to have it."""
    sys.path.insert(0, str(SOURCE_ROOT))
    from daisy.base import confinement

    state = confinement.probe()
    return bool(state["backend"]), str(state["detail"])


@contextmanager
def daemon(
    *,
    roots: Optional[dict[str, str]] = None,
    configuration: str = "",
    keep_roots: bool = False,
) -> Iterator[Daemon]:
    """A daemon of its own, on its own directories, torn down afterwards.

    `roots` is passed when a stage needs a *second* daemon over the same directories — which is
    how the restart claims are checked — and `keep_roots` stops the first one deleting what the
    second still needs.
    """
    owned = roots is None
    roots = roots or make_roots()
    if configuration:
        seed_configuration(roots, configuration)
    process = subprocess.Popen(
        [sys.executable, "-m", "daisy", "daisyd"],
        env=environment_for(roots),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    started = time.monotonic()
    port = token = ""
    runtime_directory = Path(roots["XDG_RUNTIME_DIR"]) / "daisy"
    while time.monotonic() - started < DAEMON_READY_SECONDS:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"the daemon exited during startup ({process.returncode}): {output[-2000:]}")
        try:
            port = (runtime_directory / "port").read_text().strip()
            token = (runtime_directory / "token").read_text().strip()
        except OSError:
            port = token = ""
        if port and token:
            break
        time.sleep(0.2)
    if not (port and token):
        process.kill()
        raise RuntimeError("the daemon never published its endpoint")

    instance = Daemon(roots, process, port, token)
    try:
        yield instance
    finally:
        instance.stop()
        if owned and not keep_roots:
            for path in roots.values():
                shutil.rmtree(path, ignore_errors=True)


def discard(roots: dict[str, str]) -> None:
    for path in roots.values():
        shutil.rmtree(path, ignore_errors=True)


# Confinement cannot be enforced on every machine — a container without Landlock is the common
# case — and that is an environment fact, not a defect. `preferred` keeps the resource limits
# and lets a session start, which is what the lifecycle claims are actually about.
PERMISSIVE_CONFINEMENT = "sandbox:\n  enforce: preferred\n"


__all__ = [
    "Daemon",
    "Outcome",
    "PERMISSIVE_CONFINEMENT",
    "RpcError",
    "SOURCE_ROOT",
    "confinement_available",
    "daemon",
    "discard",
    "environment_for",
    "make_roots",
    "report",
    "seed_configuration",
]
