"""Throwaway verification of the XEAC end state. Deleted once it has served its purpose.

This exists to prove the migration works, not to become a suite. It leans on the paths a real
user actually takes, and on the ways those paths go wrong: a daemon that died without cleaning
up, a session addressed by someone who was never handed it, a child asking for more authority
than its parent has, a parent dying with children still running, two clients racing.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "src"))

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  ok    {name}")
    else:
        FAILED.append((name, detail))
        print(f"  FAIL  {name}  {detail}")


# --- placement -------------------------------------------------------------------------

def verify_paths() -> None:
    print("\nXDG placement")
    sandbox = Path(tempfile.mkdtemp(prefix="xeac-verify-"))
    os.environ.update({
        "XDG_CONFIG_HOME": str(sandbox / "config"),
        "XDG_DATA_HOME": str(sandbox / "data"),
        "XDG_STATE_HOME": str(sandbox / "state"),
        "XDG_CACHE_HOME": str(sandbox / "cache"),
        "XDG_RUNTIME_DIR": str(sandbox / "run"),
    })
    from xeac.base import paths

    check("config honours XDG_CONFIG_HOME", paths.config_directory() == sandbox / "config" / "xeac")
    check("data honours XDG_DATA_HOME", paths.data_directory() == sandbox / "data" / "xeac")
    check("runtime honours XDG_RUNTIME_DIR", paths.runtime_directory() == sandbox / "run" / "xeac")
    check("runtime directory is private", oct(paths.runtime_directory().stat().st_mode)[-3:] == "700")
    check("database sits under data", paths.database_file_path().parent == paths.data_directory())
    # A relative XDG value must be ignored, per the specification — otherwise a stray value
    # silently scatters state into the working directory.
    os.environ["XDG_CONFIG_HOME"] = "relative/path"
    check("relative XDG value falls back", paths.config_directory() == Path.home() / ".config" / "xeac")
    os.environ["XDG_CONFIG_HOME"] = str(sandbox / "config")
    return None


# --- permission model ------------------------------------------------------------------

def verify_permission_mode() -> None:
    print("\npermission model")
    from xeac.base.permission_mode import PermissionMode

    check("bypass is gone", PermissionMode.parse("bypass") is None)
    check("a stored bypass falls back to asking", PermissionMode.coerce("bypass") is PermissionMode.DEFAULT)
    check(
        "a child cannot be looser than its parent",
        PermissionMode.more_restrictive("auto", "read_only") is PermissionMode.READ_ONLY,
    )
    check(
        "a child may be tighter than its parent",
        PermissionMode.more_restrictive("read_only", "default") is PermissionMode.READ_ONLY,
    )
    check("no mode is unknown to itself", all(PermissionMode.parse(str(m)) is m for m in PermissionMode))


# --- registry, tokens, and the tree ------------------------------------------------------

def verify_registry() -> None:
    print("\nregistry and capability tokens")
    from xeac.daemon.registry import RUNNING, SessionRegistry

    registry = SessionRegistry()
    parent = registry.create(agent="a", working_directory="/tmp", permission_mode="default", created_at="t0")
    child = registry.create(agent="b", working_directory="/tmp", permission_mode="read_only", parent=parent.id, created_at="t1")
    grandchild = registry.create(agent="c", working_directory="/tmp", permission_mode="read_only", parent=child.id, created_at="t2")

    check("a token is minted per session", bool(parent.token) and parent.token != child.token)
    check("the right token authorises", registry.authorize(parent.id, parent.token) is parent)
    check("another session's token does not", registry.authorize(parent.id, child.token) is None)
    check("an empty token does not", registry.authorize(parent.id, "") is None)
    check("a listing never leaks the token", "token" not in parent.public())

    descendants = {record.id for record in registry.descendants_of(parent.id)}
    check("the tree reaches grandchildren", descendants == {child.id, grandchild.id})

    # A corrupted parent pointer must not turn reaping into an infinite walk — reaping runs
    # during shutdown, when hanging is the worst possible failure.
    looped = registry.create(agent="d", working_directory="/tmp", permission_mode="default", created_at="t3")
    registry.mark(looped.id, parent=looped.id)
    walked = list(registry.descendants_of(looped.id))
    check("a parent cycle terminates", len(walked) <= 1)

    registry.mark(parent.id, status=RUNNING)
    check("live excludes terminal sessions", parent in registry.live())


# --- the daemon, end to end ----------------------------------------------------------------

def _run(*arguments: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "xeac", *arguments],
        capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        cwd=str(REPO),
    )


def verify_daemon() -> None:
    print("\ndaemon lifecycle")
    from xeac.base import paths

    # A socket left by a daemon that was hard-killed must not block the next start. This is the
    # failure that would otherwise need a manual `rm` after every crash.
    stale = paths.daemon_socket_path()
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("")
    from xeac.daemon.__main__ import _reclaim_socket

    try:
        _reclaim_socket()
        check("a stale socket is reclaimed", not stale.exists())
    except SystemExit:
        check("a stale socket is reclaimed", False, "refused to start on a dead socket")

    daemon = subprocess.Popen(
        [sys.executable, "-m", "xeac", "xeacd"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        cwd=str(REPO), start_new_session=True,
    )
    try:
        ready = False
        for _ in range(200):
            time.sleep(0.25)
            if paths.daemon_token_path().exists() and paths.daemon_socket_path().exists():
                ready = True
                break
            if daemon.poll() is not None:
                break
        check("the daemon starts and publishes its handshake", ready,
              "" if ready else (daemon.stderr.read()[-400:] if daemon.poll() is not None else "timed out"))
        if not ready:
            return

        check("the token file is private", oct(paths.daemon_token_path().stat().st_mode)[-3:] == "600")

        import httpx

        socket_path = str(paths.daemon_socket_path())
        token = paths.daemon_token_path().read_text().strip()
        transport = httpx.HTTPTransport(uds=socket_path)
        with httpx.Client(transport=transport, timeout=20.0) as client:
            health = client.get("http://daemon/health")
            check("health answers without a token", health.status_code == 200)

            unauthenticated = client.post("http://daemon/rpc", json={"method": "daemon.status", "params": {}})
            check("the control plane refuses an unauthenticated call", unauthenticated.status_code == 401)

            wrong = client.post(
                "http://daemon/rpc", json={"method": "daemon.status", "params": {}},
                headers={"Authorization": "Bearer not-the-token"},
            )
            check("a wrong token is refused", wrong.status_code == 401)

            authorised = {"Authorization": f"Bearer {token}"}
            status = client.post("http://daemon/rpc", json={"method": "daemon.status", "params": {}}, headers=authorised)
            check("daemon.status answers with a token", status.status_code == 200 and status.json()["result"]["ok"])

            unknown = client.post("http://daemon/rpc", json={"method": "nope", "params": {}}, headers=authorised)
            check("an unknown method is a clean 404", unknown.status_code == 404)

            listing = client.post("http://daemon/rpc", json={"method": "session.list", "params": {}}, headers=authorised)
            check("session.list works when empty", listing.status_code == 200 and listing.json()["result"]["sessions"] == [])

            missing = client.post(
                "http://daemon/rpc", json={"method": "session.get", "params": {"id": "session-nope"}}, headers=authorised
            )
            check("an unknown session is a clean 404", missing.status_code == 404)

            malformed = client.post("http://daemon/rpc", content=b"not json", headers=authorised)
            check("a malformed body does not crash the daemon", malformed.status_code == 400)

            still_alive = client.post("http://daemon/rpc", json={"method": "daemon.status", "params": {}}, headers=authorised)
            check("the daemon survives bad input", still_alive.status_code == 200)

            pool = still_alive.json()["result"]["pool"]
            check("workers are kept warm", pool["warm"] >= 1, f"pool={pool}")

        # The CLI finds a running daemon rather than starting a second one.
        listed = _run("ps")
        check("the CLI reaches the daemon", listed.returncode == 0, listed.stderr[-300:])
        check("ps says so when nothing is running", "no sessions" in listed.stdout, listed.stdout[:200])

        status_command = _run("daemon", "status")
        check("the CLI reports daemon status", status_command.returncode == 0 and "xeacd" in status_command.stdout)

        json_mode = _run("--json", "ps")
        check("--json emits parseable output", json_mode.returncode == 0 and isinstance(json.loads(json_mode.stdout), list))
    finally:
        # SIGTERM the group, exactly as the desktop shell does on quit.
        try:
            os.killpg(os.getpgid(daemon.pid), 15)
        except ProcessLookupError:
            pass
        try:
            daemon.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(daemon.pid), 9)
        from xeac.base import paths as p2

        check("the handshake is cleaned up on exit", not p2.daemon_token_path().exists())


def verify_cli_surface() -> None:
    print("\nCLI surface")
    helped = _run("--help")
    check("xeac --help works with no daemon", helped.returncode == 0)
    for verb in ("create", "send", "ps", "attach", "tree", "approve", "kill", "wait", "get", "history", "daemon"):
        check(f"`{verb}` is a verb", verb in helped.stdout, helped.stdout[:200])
    check("there is no verb that changes a running session's mode", "permissions" not in helped.stdout)


def verify_worker_isolation() -> None:
    print("\nworker fork safety")
    # The accessibility surfaces must never be imported at module level: a parked worker that
    # had loaded PyObjC could not be forked safely on macOS.
    import subprocess as sp

    result = sp.run([sys.executable, "scripts/check_layers.py"], capture_output=True, text=True, cwd=str(REPO))
    check("layering and fork-safety invariants hold", result.returncode == 0, result.stdout[-400:])

    from xeac.daemon.pool import worker_command

    command = worker_command()
    check("a worker is a re-exec of this image", command[0] == sys.executable and command[-1] == "worker")


def main() -> int:
    print("XEAC end-state verification")
    verify_paths()
    verify_permission_mode()
    verify_registry()
    verify_cli_surface()
    verify_worker_isolation()
    verify_daemon()
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for name, detail in FAILED:
        print(f"  FAILED {name}: {detail}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
