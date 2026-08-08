"""`langmesh reach`: one address a phone can keep, over a connection a browser will trust."""

from __future__ import annotations

import base64
import logging
import json
import os
import re
import secrets
import socket
import subprocess
from pathlib import Path
from typing import Optional

# A fixed port, because a phone has to agree on one, placed next to `langmesh serve`'s so the two read as a pair.
DEFAULT_PORT = 8825

# A `langmesh://` link rather than an https one, because the payload is a secret nothing should resolve on its own.
PAIRING_SCHEME = "langmesh"

# The session cookie the interface rides on, named for what it is so it is distinguishable in a debugger.
REACH_COOKIE = "langmesh_reach"

# Tailscale on macOS may keep its command line inside the bundle, so both places are tried.
_TAILSCALE_CANDIDATES = (
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
)


logger = logging.getLogger("langmesh.reach")


def _report(error: "TailscaleUnavailable") -> None:
    """Say what is wrong and what Tailscale said about it, never a traceback, since every one of these needs a person to act."""
    logger.info(f"langmesh: {error}")
    if error.detail:
        logger.info(f"langmesh: tailscale said: {error.detail}")


def reach_token(create: bool = True) -> Optional[str]:
    """The durable token, minted on first use, read back from disk so a rotation elsewhere is picked up."""
    from langmesh.base.paths import reach_token_path

    path = reach_token_path()
    try:
        existing = path.read_text().strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    if not create:
        return None
    return _write_token(path, secrets.token_urlsafe(32))


def rotate_token() -> str:
    """Mint a new token, which unpairs every device holding the old one."""
    from langmesh.base.paths import reach_token_path

    return _write_token(reach_token_path(), secrets.token_urlsafe(32))


def _write_token(path: Path, token: str) -> str:
    # Written to a neighbour and moved into place, opened 0600 from the start so the secret is never at the umask's mercy.
    temporary = path.with_name(path.name + ".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(token)
    temporary.replace(path)
    return token


class TailscaleUnavailable(RuntimeError):
    """Tailscale cannot front this listener, with a sentence saying what to do about it."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


def _tailscale_command() -> str:
    """Where the Tailscale command line is: on PATH when its integration is on, and inside the bundle otherwise."""
    from shutil import which

    found = which("tailscale")
    if found:
        return found
    for candidate in _TAILSCALE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise TailscaleUnavailable(
        "Tailscale is not installed. LangMesh reaches your phone over your tailnet, which is what makes the connection both stable and encrypted; install Tailscale and sign in, then run this again."
    )


# A link Tailscale prints when something has to be switched on for the whole tailnet.
_CONSOLE_LINK = re.compile(r"https://login\.tailscale\.com/\S+")


def _tailscale(*arguments: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [_tailscale_command(), *arguments],
            capture_output=True,
            text=True,
            # Nothing here can answer a prompt, and a command silently waiting on one looks exactly like one that has hung.
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        # What it managed to say before the clock ran out, which is usually the whole answer.
        said = _said(error.stdout) + _said(error.stderr)
        if (link := _CONSOLE_LINK.search(said)) is not None:
            raise TailscaleUnavailable(
                "Tailscale is waiting for something to be switched on for your tailnet. Open this, turn it on, then run this again.",
                link.group(0),
            ) from error
        raise TailscaleUnavailable(
            f"Tailscale did not answer `{' '.join(arguments)}` within {timeout:.0f}s. Open the Tailscale app and check it is connected.",
            " ".join(said.split()),
        ) from error
    except OSError as error:
        raise TailscaleUnavailable(f"Tailscale would not run: {error}") from error


def _said(stream) -> str:
    """Whatever a stream held, as text. `TimeoutExpired` carries bytes or `None`."""
    if stream is None:
        return ""
    return stream.decode("utf-8", "replace") if isinstance(stream, bytes) else str(stream)


def tailnet_name() -> str:
    """This machine's MagicDNS name on the tailnet, which is the name the certificate is issued for."""
    completed = _tailscale("status", "--json", timeout=10.0)
    if completed.returncode != 0:
        raise TailscaleUnavailable(
            "Tailscale is installed but not connected. Open the Tailscale app and sign in, then run this again."
        )
    try:
        status = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise TailscaleUnavailable("Tailscale reported a status this could not read.") from error

    if str(status.get("BackendState") or "") != "Running":
        raise TailscaleUnavailable(
            "Tailscale is installed but not connected. Open the Tailscale app and sign in, then run this again."
        )
    # Fully qualified with a trailing dot, which is correct for DNS and wrong in a URL.
    name = str((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
    if not name:
        raise TailscaleUnavailable(
            "This machine has no MagicDNS name, so there is no address a certificate can be issued for. Turn MagicDNS on in the Tailscale admin console under DNS."
        )
    # Checked separately from the name, because with MagicDNS off the name resolves nowhere and no certificate is issued.
    if not (status.get("CurrentTailnet") or {}).get("MagicDNSEnabled"):
        raise TailscaleUnavailable(
            "MagicDNS is off for your tailnet, so this machine's name resolves nowhere and Tailscale will not issue a certificate for it. Turn on MagicDNS in the admin console under DNS, and then HTTPS Certificates below it — in that order, because the second is only available once the first is on.",
            "https://login.tailscale.com/admin/dns",
        )
    return name


def ensure_served(port: int) -> None:
    """Put this listener on the tailnet over HTTPS if it is not there already, in the background so this command keeps its terminal."""
    # Twenty seconds rather than a minute: configuring a serve is instant when it works, so a longer wait only delays the reason.
    completed = _tailscale("serve", "--bg", "--https=443", f"http://127.0.0.1:{port}", timeout=20.0)
    if completed.returncode == 0:
        return
    message = (completed.stderr or completed.stdout or "").strip()
    # The one failure worth naming, because certificates are off for the whole tailnet until somebody turns them on.
    if (link := _CONSOLE_LINK.search(message)) is not None:
        raise TailscaleUnavailable(
            "Tailscale is waiting for something to be switched on for your tailnet. Open this, turn it on, then run this again.",
            link.group(0),
        )
    lowered = message.lower()
    if "https" in lowered and ("enable" in lowered or "certificate" in lowered):
        raise TailscaleUnavailable(
            "Your tailnet does not have HTTPS certificates enabled, so Tailscale cannot get a certificate for this machine. Turn it on in the admin console under DNS, then HTTPS, Certificates, then run this again.",
            message,
        )
    raise TailscaleUnavailable("Tailscale would not serve this listener.", message)


def pairing_payload() -> dict:
    """What a phone is handed, once: one address rather than a list of candidates to race."""
    return {
        "version": 1,
        # The first label only, since a hostname arrives with whatever the network's DHCP server decided to append.
        "name": socket.gethostname().split(".")[0],
        "token": reach_token(),
        # Port 443, so it is not in the URL, where `tailscale serve` listens and nothing else competes.
        "endpoint": f"https://{tailnet_name()}",
    }


def pairing_uri(payload: dict) -> str:
    """The payload as a single link, in the fragment so it is never sent to a server or written to a log."""
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    return f"{PAIRING_SCHEME}://pair#{encoded.rstrip('=')}"


def require_token(application, token: str):
    """Wrap an application so nothing reaches it without the reach token, as pure ASGI so long-lived streams survive."""
    import secrets as _secrets
    from urllib.parse import parse_qsl, urlencode

    async def refuse(scope, receive, send) -> None:
        if scope["type"] == "websocket":
            # Closing before accepting is how ASGI refuses a handshake, which surfaces as a failed upgrade rather than a dead socket.
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps(
            {"error": {"code": "unauthorized", "message": "Bad or missing reach token."}},
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def is_preflight(scope) -> bool:
        """A preflight is sent without credentials by specification, so demanding a token rejects the question rather than the request."""
        if scope["type"] != "http" or scope.get("method") != "OPTIONS":
            return False
        return any(
            name.lower() == b"access-control-request-method"
            for name, _ in scope.get("headers") or []
        )

    async def guarded(scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await application(scope, receive, send)
        if is_preflight(scope):
            return await application(scope, receive, send)
        presented, remainder, from_query = _presented_token(scope, parse_qsl, urlencode)
        if not presented or not _secrets.compare_digest(presented, token):
            return await refuse(scope, receive, send)
        scope = dict(scope, query_string=remainder, headers=_without_cookie(scope))

        # A document asked for with a token is the app opening the interface, so answer it and set the cookie on the way out.
        if from_query and scope["type"] == "http" and _wants_document(scope):
            return await application(scope, receive, _setting_cookie(send, presented))
        return await application(scope, receive, send)

    return guarded


def _wants_document(scope) -> bool:
    """Whether this request is a page rather than something a page asked for, from `Sec-Fetch-Dest` when the browser sends it."""
    headers = {name.lower(): value for name, value in scope.get("headers") or []}
    destination = headers.get(b"sec-fetch-dest", b"").decode("latin-1")
    if destination:
        return destination == "document"
    return b"text/html" in headers.get(b"accept", b"")


def _setting_cookie(send, token: str):
    """Wrap `send` so the response that goes out carries the session cookie."""

    async def sending(message):
        if message["type"] == "http.response.start":
            message = dict(message)
            # `HttpOnly`, `SameSite=Lax`, `Secure` and session-scoped, so no script reads it, no other site spends it and closing the app ends it.
            cookie = f"{REACH_COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Lax"
            message["headers"] = [
                *message.get("headers", []),
                (b"set-cookie", cookie.encode("latin-1")),
            ]
        await send(message)

    return sending


def _without_cookie(scope) -> list:
    """The request headers with our cookie taken out, so it never reaches the daemon."""
    from http.cookies import SimpleCookie

    kept = []
    for name, value in scope.get("headers") or []:
        if name.lower() != b"cookie":
            kept.append((name, value))
            continue
        jar = SimpleCookie()
        jar.load(value.decode("latin-1"))
        remaining = "; ".join(
            f"{key}={entry.value}" for key, entry in jar.items() if key != REACH_COOKIE
        )
        if remaining:
            kept.append((name, remaining.encode("latin-1")))
    return kept


def _presented_token(scope, parse_qsl, urlencode) -> tuple[str, bytes, bool]:
    """The token the caller offered, the query string without it, and whether it came from there."""
    from http.cookies import SimpleCookie

    headers = {name.lower(): value for name, value in scope.get("headers") or []}

    authorization = headers.get(b"authorization", b"").decode("latin-1")
    if authorization.startswith("Bearer "):
        return authorization[len("Bearer ") :], scope.get("query_string", b""), False

    pairs = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
    presented = next((value for key, value in pairs if key == "token"), "")
    if presented:
        remainder = urlencode([(key, value) for key, value in pairs if key != "token"])
        return presented, remainder.encode("latin-1"), True

    if b"cookie" in headers:
        jar = SimpleCookie()
        jar.load(headers[b"cookie"].decode("latin-1"))
        entry = jar.get(REACH_COOKIE)
        if entry is not None:
            return entry.value, scope.get("query_string", b""), False

    return "", scope.get("query_string", b""), False


def _describe(payload: dict) -> None:
    """Say how to pair a device: the link is data and goes to stdout, and the prose about it goes to stderr."""
    logger.info(f"Pair a device with LangMesh on {payload['name']}, at {payload['endpoint']}.")
    logger.info(
        "This link carries a token with full control of this daemon. Send it to a phone, not to a room."
    )
    print(pairing_uri(payload), flush=True)


def run(arguments) -> int:
    """Serve, or print a pairing code, or rotate the token."""
    action = getattr(arguments, "action", "serve") or "serve"

    if action == "rotate":
        rotate_token()
        logger.info("Rotated. Every paired device must pair again.")
        return 0

    try:
        payload = pairing_payload()
    except TailscaleUnavailable as error:
        _report(error)
        return 1

    if action == "pair":
        _describe(payload)
        return 0

    return _serve(arguments, payload)


def _serve(arguments, payload: dict) -> int:
    import uvicorn

    from langmesh.base.paths import daemon_port_path, daemon_token_path
    from langmesh.cli.client import ensure_daemon
    from langmesh.cli.commands.serve import (
        _port_is_taken,
        build_application,
        interface_directory,
    )

    # Loopback always, with no flag to change it, because Tailscale is what carries this off the machine.
    host = "127.0.0.1"
    if _port_is_taken(host, arguments.port):
        logger.info(
            f"langmesh: {host}:{arguments.port} is already in use — most likely another `langmesh reach`. Stop it, or pass `--port` to use a different one."
        )
        return 1

    # Started if it is not up and left running when this stops, unlike `langmesh serve`, because reaching outlives the terminal.
    ensure_daemon()
    try:
        daemon_port = int(daemon_port_path().read_text().strip())
        daemon_token = daemon_token_path().read_text().strip()
    except (OSError, ValueError):
        logger.info("langmesh: langmeshd is not running and could not be started.")
        return 1

    # The interface and the proxy together, since the phone's app is a window onto that interface rather than a second one.
    develop = getattr(arguments, "interface", "") or ""
    interface = None if develop else interface_directory()
    if develop:
        logger.info(
            f"langmesh: serving the interface from {develop} — changes reload without a build."
        )
    elif interface is None:
        logger.info(
            "langmesh: the interface has not been built, so this will serve the control plane but no screens. Run `cd web && bun run build` in a checkout, or install the packaged build."
        )

    def where_is_the_daemon() -> tuple[str, str]:
        """The daemon's address and token, read fresh, so a daemon restarting beneath this proxy needs nothing re-paired."""
        return (
            f"http://127.0.0.1:{int(daemon_port_path().read_text().strip())}",
            daemon_token_path().read_text().strip(),
        )

    application = build_application(
        f"http://127.0.0.1:{daemon_port}",
        daemon_token,
        interface,
        interface_url=develop,
        rediscover=where_is_the_daemon,
    )
    guarded = require_token(application, payload["token"])

    # Put it on the tailnet before saying it is available, so a failure is reported rather than scanned into.
    try:
        ensure_served(arguments.port)
    except TailscaleUnavailable as error:
        _report(error)
        return 1

    _describe(payload)
    logger.info(
        f"Serving on {payload['endpoint']}. Scan the code with LangMesh on your phone, or paste the link."
    )

    # No TLS here: Tailscale terminates it with a certificate issued for this machine's tailnet name.
    configuration = uvicorn.Config(
        guarded,
        host=host,
        port=arguments.port,
        log_level="warning",
    )
    uvicorn.Server(configuration).run()
    return 0
