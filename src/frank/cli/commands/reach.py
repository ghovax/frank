"""`frank reach`: one address a phone can keep, and a token that outlives a reboot.

The daemon is deliberately unreachable. It binds loopback on a port it picks fresh every boot,
and mints a capability token to match, so nothing off this machine can address it and nothing
on it can address it without reading a 0600 file. That is the right default and this does not
change it.

But a phone is off this machine, and a phone cannot be told a new address every morning. What
it needs is the opposite of what the daemon offers: a port that is the same tomorrow, a token
that is the same next month, and a way to be handed both once. So this is a second front door —
a proxy, in the same shape as `frank serve`, with three differences that are the whole point:

  1. **It authenticates.** `frank serve` has no auth of its own; it is loopback-only and the
     browser is on the same machine. This one is meant to leave the machine, so a request
     without the reach token gets a 401 and never touches the daemon. Websockets too, which is
     the case an HTTP-shaped check forgets.
  2. **Its token is durable.** Kept in the data directory, minted once, rotated on request.
     Pairing a device against the daemon's own token would unpair it at the next restart.
  3. **It knows where it can be found.** `frank reach pair` enumerates the addresses this
     machine actually answers on — the Tailscale one first, because that is the only one in the
     list that is both stable and encrypted — and prints them, with the token, as one QR code.

What it deliberately does not do is make this safe to put on the public internet. It is a
bearer token over whatever transport you gave it, which is exactly what `SECURITY.md` says to
tunnel rather than expose. The recommended shape is Tailscale: the address is stable for the
life of the machine, WireGuard carries the token, and nothing is listening on a public port at
all. A reverse proxy terminating TLS on a hostname you own is the same bargain differently
bought, and `--advertise` is how you tell a phone about it.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional

# The port. Fixed, and that is its only interesting property: everything else in Frank takes an
# ephemeral port precisely so that nothing has to agree on one, and this exists because a phone
# has to. Next to `frank serve`'s 8824 so the two read as a pair.
DEFAULT_PORT = 8825

# Where a paired device is told to go. The scheme is `frank://` rather than an https URL because
# the payload is a secret: an https link would be resolved by whatever handles links on the
# phone — a browser, a preview fetcher, a chat client's unfurler — and the token would go with
# it. A private scheme is opened by this application or by nothing.
PAIRING_SCHEME = "frank"

# The session cookie the interface rides on. Named for what it is, so somebody looking at a
# request in a debugger can tell it from the daemon's own credentials.
REACH_COOKIE = "frank_reach"

# Tailscale on macOS may be the App Store build, which puts its command line inside the bundle
# rather than on PATH. Both are tried before concluding there is no Tailscale here.
_TAILSCALE_CANDIDATES = (
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
)


def _note(message: str) -> None:
    print(message, file=sys.stderr)


def reach_token(create: bool = True) -> Optional[str]:
    """The durable token, minting one on first use.

    Written 0600 and read back rather than cached in memory, so a rotation by another process is
    picked up by the next `pair` without anything having to be told."""
    from frank.base.paths import reach_token_path

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
    from frank.base.paths import reach_token_path

    return _write_token(reach_token_path(), secrets.token_urlsafe(32))


def _write_token(path: Path, token: str) -> str:
    # Written to a neighbour and moved into place, so a reader never sees a half-written token —
    # and opened 0600 from the start rather than chmod'ed after, which would leave a window in
    # which the secret existed at the umask's mercy.
    temporary = path.with_name(path.name + ".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(token)
    temporary.replace(path)
    return token


def _tailscale_address() -> Optional[str]:
    """This machine's Tailscale name, or ``None`` if it is not on a tailnet.

    The MagicDNS name in preference to the 100.x address: both are stable, but the name survives
    the machine being re-added to the tailnet and is the one a person can read back off a screen.
    """
    from shutil import which

    command = which("tailscale")
    if command is None:
        command = next((path for path in _TAILSCALE_CANDIDATES if Path(path).is_file()), None)
    if command is None:
        return None
    try:
        completed = subprocess.run(
            [command, "status", "--json"], capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        status = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    myself = status.get("Self") or {}
    # `DNSName` arrives fully qualified with a trailing dot, which is correct for DNS and wrong
    # in a URL.
    name = str(myself.get("DNSName") or "").rstrip(".")
    if name:
        return name
    for address in myself.get("TailscaleIPs") or []:
        if ":" not in str(address):
            return str(address)
    return None


def _local_address() -> Optional[str]:
    """The address this machine uses to reach the rest of its network.

    Found by asking the routing table rather than by enumerating interfaces: a connected UDP
    socket sends nothing, but the kernel has to choose a source address to answer
    `getsockname()`, and that choice *is* the answer to "which of my addresses faces the
    network". Enumerating gets you loopback, every bridge Docker ever made, and no way to rank
    them."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.settimeout(0.5)
        try:
            # A documentation-range address, chosen because nothing is expected to answer and
            # nothing needs to: no packet leaves the host for an unconnected UDP socket.
            probe.connect(("192.0.2.1", 9))
            address = probe.getsockname()[0]
        except OSError:
            return None
    return address if address and not address.startswith("127.") else None


def endpoints(port: int, secure: bool, advertise: str = "") -> list[str]:
    """Every address this listener can be reached at, best first.

    "Best" means most likely to still work tomorrow, from somewhere else. An address the caller
    advertised wins because they know something this code cannot; Tailscale comes next because
    it is stable *and* encrypted *and* needs no port open anywhere; the LAN address is last
    because it is none of those things and is offered as the thing that works today, at home."""
    scheme = "https" if secure else "http"
    found: list[str] = []
    if advertise:
        # Taken as given if it carries a scheme — the caller may be describing a reverse proxy on
        # 443, where appending this listener's port would be wrong.
        found.append(advertise if "://" in advertise else f"{scheme}://{advertise}:{port}")
    if (tailnet := _tailscale_address()) is not None:
        found.append(f"{scheme}://{tailnet}:{port}")
    if (local := _local_address()) is not None:
        found.append(f"{scheme}://{local}:{port}")
    return found


def pairing_payload(port: int, secure: bool, advertise: str = "") -> dict:
    return {
        "version": 1,
        # The first label only. A hostname arrives with whatever the network's DHCP server
        # decided to append, which on a home router is the ISP's domain — so the machine a
        # person calls "Giovanni's MBP" would introduce itself to the phone by the name of a
        # telephone company.
        "name": socket.gethostname().split(".")[0],
        "token": reach_token(),
        "endpoints": endpoints(port, secure, advertise),
    }


def pairing_uri(payload: dict) -> str:
    """The payload as a single link.

    In the fragment, not the query. A fragment is not sent to a server by anything that resolves
    a URL, is not written to proxy logs, and is not part of what a QR reader would show as the
    "site" — which for a link carrying a bearer token is worth the three extra characters."""
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    return f"{PAIRING_SCHEME}://pair#{encoded.rstrip('=')}"


def require_token(application, token: str):
    """Wrap an application so nothing reaches it without the reach token.

    An ASGI class rather than Starlette's `BaseHTTPMiddleware`, for the reason the daemon gives
    for the same choice: this proxy's whole job is streams that stay open for hours, and that
    middleware's cancel scopes do not survive them. A plain callable has no such opinion.

    Three ways to present it, and each exists for a transport that cannot manage the others:

      - the `Authorization` header, for anything making its own requests;
      - a `token` query parameter, because a websocket handshake cannot carry a header;
      - a **cookie**, because a *page* cannot carry either.

    The cookie is what lets this serve the interface. A browser — or the phone's webview — asks
    for a document, then for every script, font, event stream and websocket that document names,
    and it attaches nothing of its own to any of them. Handing the page a token to attach would
    mean the token living in reachable storage on the device; a cookie is sent by the transport,
    is `HttpOnly` so no script can read it, and covers subresources and upgrades alike. So the
    app opens `…/?token=…` exactly once, this exchanges it for the cookie, and everything after
    that is an ordinary same-origin request.

    Whichever form it arrived in, it is **removed before the request goes any further**: the query
    parameter is stripped and the cookie header is dropped. Forwarding either would put this
    listener's durable secret into the daemon's logs and — worse, for the query form — into the
    daemon's own token check, where it would shadow the header the proxy is about to attach."""
    import secrets as _secrets
    from urllib.parse import parse_qsl, urlencode

    async def refuse(scope, receive, send) -> None:
        if scope["type"] == "websocket":
            # Closing before accepting is how ASGI says "the handshake is refused", and it
            # surfaces to the client as a failed upgrade rather than a socket that opens and
            # then dies for no stated reason.
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps(
            {"error": {"code": "unauthorized", "message": "Bad or missing reach token."}},
        ).encode()
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})

    def is_preflight(scope) -> bool:
        """A CORS preflight, which a browser sends without credentials by specification.

        Demanding a token here rejects the *question* rather than the request, and the real
        request — the one that would have carried the token — is then never sent at all. The
        daemon's own middleware carries this exemption for the same reason and says so at
        greater length; this is the second place it has to hold, because a preflight that
        reaches the daemon has already had to get past this.
        """
        if scope["type"] != "http" or scope.get("method") != "OPTIONS":
            return False
        return any(name.lower() == b"access-control-request-method" for name, _ in scope.get("headers") or [])

    async def guarded(scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await application(scope, receive, send)
        if is_preflight(scope):
            return await application(scope, receive, send)
        presented, remainder, from_query = _presented_token(scope, parse_qsl, urlencode)
        if not presented or not _secrets.compare_digest(presented, token):
            return await refuse(scope, receive, send)
        scope = dict(scope, query_string=remainder, headers=_without_cookie(scope))

        # A document asked for with `?token=…` is the app opening the interface. Answer it, and
        # set the cookie on the way out, so the hundred requests that document is about to make
        # carry the token without anything having to remember to add it.
        if from_query and scope["type"] == "http" and _wants_document(scope):
            return await application(scope, receive, _setting_cookie(send, presented))
        return await application(scope, receive, send)

    return guarded


def _wants_document(scope) -> bool:
    """Whether this request is a page rather than something a page asked for.

    `Sec-Fetch-Dest` says so outright and every current browser sends it. Without it, a request
    that accepts HTML is close enough — the only cost of guessing wrong is a cookie set on
    something that did not need one."""
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
            # `HttpOnly` so no script on the page can read it back out, `SameSite=Lax` so another
            # site cannot make the browser spend it, and session-scoped so closing the app ends
            # it. Not `Secure`: on a tailnet this is plain HTTP by design, and a `Secure` cookie
            # would simply never be stored there.
            cookie = f"{REACH_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"
            message["headers"] = [*message.get("headers", []), (b"set-cookie", cookie.encode("latin-1"))]
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
    """The token the caller offered, the query string without it, and whether it came from there.

    The last of those is what decides whether to answer with a cookie: a token in the query is
    the app opening the interface and asking to be let in for the session, while one in a header
    or a cookie is a caller that already carries it and needs nothing back.
    """
    from http.cookies import SimpleCookie

    headers = {name.lower(): value for name, value in scope.get("headers") or []}

    authorization = headers.get(b"authorization", b"").decode("latin-1")
    if authorization.startswith("Bearer "):
        return authorization[len("Bearer "):], scope.get("query_string", b""), False

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


def _describe(payload: dict, port: int, host: str) -> None:
    """Print what a person needs to pair a device, and the QR code that saves them typing it."""
    import segno

    uri = pairing_uri(payload)
    print(f"Pair a device with Frank on {payload['name']}:\n")
    if not payload["endpoints"]:
        _note(
            "frank: this machine has no address but loopback, so nothing off it can reach this "
            "listener. Join a tailnet, or pass --advertise with the address of whatever fronts "
            "this."
        )
    for index, endpoint in enumerate(payload["endpoints"]):
        marker = "  →" if index == 0 else "   "
        print(f"{marker} {endpoint}")
    print()
    segno.make(uri, error="m").terminal(compact=True)
    print(f"\n{uri}\n")
    # Flushed, because this is printed by a command that then blocks forever. Python buffers
    # stdout when it is not a terminal, so under `frank reach | tee` or a supervisor's log the
    # pairing code — the one thing this command exists to show — did not appear at all until the
    # server was stopped.
    print(f"Serving on {host}:{port}. Scan the code with Frank on your phone, or paste the link.", flush=True)
    _note(
        "frank: that code carries a token with full control of this daemon. Show it to a phone, "
        "not to a room."
    )


def run(arguments) -> int:
    """Serve, or print a pairing code, or rotate the token."""
    action = getattr(arguments, "action", "serve") or "serve"

    if action == "rotate":
        rotate_token()
        print("Rotated. Every paired device must pair again.")
        return 0

    secure = bool(getattr(arguments, "tls_certificate", "") and getattr(arguments, "tls_key", ""))
    payload = pairing_payload(arguments.port, secure, getattr(arguments, "advertise", "") or "")

    if action == "pair":
        _describe(payload, arguments.port, arguments.host)
        return 0

    return _serve(arguments, payload, secure)


def _serve(arguments, payload: dict, secure: bool) -> int:
    import uvicorn

    from frank.base.paths import daemon_port_path, daemon_token_path
    from frank.cli.client import ensure_daemon
    from frank.cli.commands.serve import (
        GRACEFUL_SHUTDOWN_SECONDS,
        _port_is_taken,
        build_application,
        interface_directory,
    )

    if _port_is_taken(arguments.host, arguments.port):
        _note(
            f"frank: {arguments.host}:{arguments.port} is already in use — most likely another "
            f"`frank reach`. Stop it, or pass `--port` to use a different one."
        )
        return 1

    # Started if it is not up, and left running when this stops — unlike `frank serve`, which
    # undoes its own side effect. The difference is what the two commands are for: serving is
    # something you do while you are looking at the screen, and reaching is something a machine
    # does so that you can be somewhere else. A phone that started a session and put the phone
    # away should not lose it because the listener was restarted.
    ensure_daemon()
    try:
        daemon_port = int(daemon_port_path().read_text().strip())
        daemon_token = daemon_token_path().read_text().strip()
    except (OSError, ValueError):
        _note("frank: frankd is not running and could not be started.")
        return 1

    # The interface *and* the proxy, because the phone's app is a window onto that interface
    # rather than a second implementation of it. This is what the cookie above exists for: the
    # bundle authenticates by being on the same machine as the daemon and so carries no reach
    # token of its own, and the cookie supplies one to every request it makes without the page
    # ever holding it.
    interface = interface_directory()
    if interface is None:
        _note(
            "frank: the interface has not been built, so this will serve the control plane but no "
            "screens. Run `cd web && bun run build` in a checkout, or install the packaged build."
        )
    application = build_application(f"http://127.0.0.1:{daemon_port}", daemon_token, interface)
    guarded = require_token(application, payload["token"])

    _describe(payload, arguments.port, arguments.host)
    if not secure and not any(endpoint.startswith("https://") for endpoint in payload["endpoints"]):
        _note(
            "frank: this is plain HTTP. On a tailnet that is fine — WireGuard is the encryption. "
            "Anywhere else, put TLS in front of it or pass --tls-certificate and --tls-key."
        )

    configuration = uvicorn.Config(
        guarded, host=arguments.host, port=arguments.port, log_level="warning",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        ssl_certfile=getattr(arguments, "tls_certificate", "") or None,
        ssl_keyfile=getattr(arguments, "tls_key", "") or None,
    )
    uvicorn.Server(configuration).run()
    return 0
