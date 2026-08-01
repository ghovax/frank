"""`frank reach`: one address a phone can keep, over a connection a browser will trust.

The daemon is deliberately unreachable. It binds loopback on a port it picks fresh every boot,
and mints a capability token to match, so nothing off this machine can address it and nothing on
it can address it without reading a 0600 file. That is the right default and this does not
change it — this binds loopback too. Nothing here ever listens on a network interface.

What faces the network is Tailscale, and only Tailscale. `tailscale serve` puts a listener on
the tailnet, terminates TLS with a Let's Encrypt certificate for this machine's `*.ts.net` name,
and proxies to the loopback port below. Three things follow, and they are the reason this is
built this way rather than on a LAN address and a bearer token in the clear:

  1. **The address is stable.** `mac.tailnet.ts.net` is the machine's name for as long as it is
     on the tailnet. A LAN address is a DHCP lease: it changes, and a phone holding the old one
     has no way to find the new one short of pairing again.
  2. **It is a *secure context*.** This is the one that is not negotiable. Browsers withhold a
     growing list of APIs from pages served over plain HTTP to anything but localhost — the
     microphone, the clipboard, `crypto.randomUUID` — and the interface is a real web
     application that uses them. Served over http://192.168.x.x it does not degrade, it breaks,
     one API at a time, with errors that read as faults in Frank.
  3. **Nothing is exposed.** No port is open on the LAN, nothing is forwarded at the router, and
     the tailnet is WireGuard between authenticated devices. The bearer token below is a second
     lock on a door that is already inside the house.

`tailscale funnel` would put this on the public internet and is deliberately not used: the token
is a bearer credential with full control of the machine, which is exactly what `SECURITY.md`
says to tunnel rather than expose.

The daemon knows none of this, and should not. It is loopback-only with a capability token; this
is the single network-facing piece; Tailscale fronts this. Each layer's reach is a property of
how it binds, not of a setting somebody could get wrong.
"""

from __future__ import annotations

import base64
import contextlib
import logging
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


logger = logging.getLogger("frank.reach")


def _report(error: "TailscaleUnavailable") -> None:
    """Say what is wrong and, underneath, what Tailscale said about it.

    Never a traceback. Every one of these is a person needing to do something in an app, and a
    stack of frames from inside `subprocess` is noise in front of the sentence that answers it."""
    logger.info(f"frank: {error}")
    if error.detail:
        logger.info(f"frank: tailscale said: {error.detail}")


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


class TailscaleUnavailable(RuntimeError):
    """Tailscale cannot front this listener, with a sentence saying what to do about it.

    A single exception rather than a set of them because every one of these is answered the same
    way — by a person doing something in the Tailscale app — and what differs is only which
    thing. The message is the whole value.

    `detail` is what Tailscale itself said, when it said anything, and it is a separate field
    rather than a second line of the message because an exception's message is one line here —
    `frank.base.errors.summary` collapses newlines precisely so it stays one field in a log. How
    the two are laid out is the caller's decision, not this class's."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


def _tailscale_command() -> str:
    """Where the Tailscale command line is.

    On PATH if the app's CLI integration is switched on, which installs a launcher into
    `/usr/local/bin`. Inside the bundle if it is not, and inside the bundle is where the App
    Store build keeps it regardless — so both are looked for before concluding there is none."""
    from shutil import which

    found = which("tailscale")
    if found:
        return found
    for candidate in _TAILSCALE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise TailscaleUnavailable(
        "Tailscale is not installed. Frank reaches your phone over your tailnet, which is what "
        "makes the connection both stable and encrypted; install Tailscale and sign in, then run "
        "this again."
    )


def _tailscale(*arguments: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [_tailscale_command(), *arguments], capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise TailscaleUnavailable(
            f"Tailscale did not answer `{' '.join(arguments)}` within {timeout:.0f}s. Open the "
            "Tailscale app and check it is connected."
        ) from error
    except OSError as error:
        raise TailscaleUnavailable(f"Tailscale would not run: {error}") from error


def tailnet_name() -> str:
    """This machine's name on the tailnet, e.g. `mac.tailnet-name.ts.net`.

    The MagicDNS name rather than the 100.x address, and not because it reads better: the
    certificate `tailscale serve` obtains is issued *for* this name, so it is the only address at
    which the connection is both encrypted and trusted. An IP would be a certificate error."""
    completed = _tailscale("status", "--json", timeout=10.0)
    if completed.returncode != 0:
        raise TailscaleUnavailable(
            "Tailscale is installed but not connected. Open the Tailscale app and sign in, then "
            "run this again."
        )
    try:
        status = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise TailscaleUnavailable("Tailscale reported a status this could not read.") from error

    if str(status.get("BackendState") or "") != "Running":
        raise TailscaleUnavailable(
            "Tailscale is installed but not connected. Open the Tailscale app and sign in, then "
            "run this again."
        )
    # Fully qualified with a trailing dot, which is correct for DNS and wrong in a URL.
    name = str((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
    if not name:
        raise TailscaleUnavailable(
            "This machine has no MagicDNS name, so there is no address a certificate can be "
            "issued for. Turn MagicDNS on in the Tailscale admin console under DNS."
        )
    return name


def ensure_served(port: int) -> None:
    """Put this listener on the tailnet over HTTPS, if it is not there already.

    `--bg` because the alternative holds the terminal for as long as the proxy exists, and this
    command has its own server to run. The configuration is Tailscale's and outlives this
    process, which is the behaviour wanted: a phone that started a session should not lose the
    machine because the listener was restarted.

    Asked for every time rather than only when absent. It is idempotent, it costs one local call,
    and it is what repairs the case where somebody ran `tailscale serve reset` — which otherwise
    presents as a phone that cannot connect and a listener insisting it is up.

    Nothing here undoes it when this command stops, and that is deliberate rather than an
    omission. The configuration is Tailscale's, it costs nothing while this is not running — the
    address answers with a connection error, which is what a phone shows as "not answering"
    either way — and re-asserting it on every start is cheaper than a teardown that could only
    ever be best-effort. `tailscale serve --https=443 off` removes it."""
    completed = _tailscale("serve", "--bg", "--https=443", f"http://127.0.0.1:{port}", timeout=60.0)
    if completed.returncode == 0:
        return
    message = (completed.stderr or completed.stdout or "").strip()
    # The one failure worth naming, because it is the one a person cannot guess: certificates are
    # off for the whole tailnet until somebody turns them on, and every machine on it fails this
    # way until they do.
    lowered = message.lower()
    if "https" in lowered and ("enable" in lowered or "certificate" in lowered):
        raise TailscaleUnavailable(
            "Your tailnet does not have HTTPS certificates enabled, so Tailscale cannot get a "
            "certificate for this machine. Turn it on in the admin console under DNS → HTTPS "
            "Certificates, then run this again.",
            message,
        )
    raise TailscaleUnavailable("Tailscale would not serve this listener.", message)


def pairing_payload() -> dict:
    """What a phone is handed, once.

    One address, not a list. There used to be a ranked set — an advertised one, the tailnet, the
    LAN — and a client that raced them to see which answered. All of that was machinery for
    coping with addresses that might not work, and the answer to an address that might not work
    is not to carry three of them; it is to use the one that does not change."""
    return {
        "version": 1,
        # The first label only. A hostname arrives with whatever the network's DHCP server
        # decided to append, which on a home router is the ISP's domain — so the machine a
        # person calls "Giovanni's MBP" would introduce itself to the phone by the name of a
        # telephone company.
        "name": socket.gethostname().split(".")[0],
        "token": reach_token(),
        # Port 443, so it is not in the URL. `tailscale serve` listens there and nothing else on
        # the tailnet competes for it.
        "endpoint": f"https://{tailnet_name()}",
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
            # `HttpOnly` so no script on the page can read it back out, `SameSite=Lax` so
            # another site cannot make the browser spend it, `Secure` so it is never sent in the
            # clear, and session-scoped so closing the app ends it.
            #
            # `Secure` is new and is the point of everything above it: this used to be plain HTTP
            # on a LAN address, where a `Secure` cookie would simply never be stored. Tailscale
            # terminates TLS in front of this now, so the browser both accepts the flag and
            # enforces it.
            cookie = f"{REACH_COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Lax"
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


def _write_image(uri: str, destination: str) -> Path:
    """Save the pairing code as a PNG, and open it.

    A QR drawn in half-block characters needs a terminal of the right size, the right font and
    the right colours, and a phone camera that will focus on glowing text — and when any of those
    is not true it degrades to a wall of noise rather than to a smaller code. An image has none
    of those requirements: it opens in a viewer, at whatever size the window is, in black on
    white, which is what a camera was built to read.

    `scale` is pixels per module; large enough to lock on from a comfortable distance, with the
    quiet zone the specification asks for so the code is not flush against the window edge.
    """
    import segno

    path = Path(destination).expanduser()
    if path.is_dir():
        path = path / "frank-pairing.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    segno.make(uri, error="m").save(path, scale=12, border=4, dark="#000000", light="#ffffff")
    if sys.platform == "darwin":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["open", str(path)], check=False, timeout=5)
    return path


def _describe(payload: dict, image: str = "") -> None:
    """Say how to pair a device.

    Split the way the rest of this command line splits it: the link is *data* and goes to stdout,
    on one line, so `frank reach pair | pbcopy` does the obvious thing. Everything else is prose
    about what to do with it and goes to stderr, so a reader never has to filter sentences out of
    the thing it came for.

    There is no QR drawn in the terminal. One needs the right window size, the right font and the
    right colours to be scannable at all, and when any of those is wrong it degrades to a wall of
    noise rather than to a smaller code — so it was a picture that mostly could not be used, in
    the middle of the output that could. `--image` writes a real PNG and opens it, which is what
    a camera was built to read."""
    logger.info(f"Pair a device with Frank on {payload['name']}, at {payload['endpoint']}.")
    if image:
        logger.info(f"Pairing code saved to {_write_image(pairing_uri(payload), image)}.")
    logger.info(
        "This link carries a token with full control of this daemon. Send it to a phone, not to "
        "a room."
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
        _describe(payload, getattr(arguments, "image", "") or "")
        return 0

    return _serve(arguments, payload)


def _serve(arguments, payload: dict) -> int:
    import uvicorn

    from frank.base.paths import daemon_port_path, daemon_token_path
    from frank.cli.client import ensure_daemon
    from frank.cli.commands.serve import (
        _port_is_taken,
        build_application,
        interface_directory,
    )

    # Loopback, always, and there is no flag to change it. Nothing here is meant to be addressed
    # from a network — Tailscale is what carries this off the machine, and it reaches this port
    # the same way anything else on the machine would.
    host = "127.0.0.1"
    if _port_is_taken(host, arguments.port):
        logger.info(
            f"frank: {host}:{arguments.port} is already in use — most likely another "
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
        logger.info("frank: frankd is not running and could not be started.")
        return 1

    # The interface *and* the proxy, because the phone's app is a window onto that interface
    # rather than a second implementation of it. This is what the cookie above exists for: the
    # bundle authenticates by being on the same machine as the daemon and so carries no reach
    # token of its own, and the cookie supplies one to every request it makes without the page
    # ever holding it.
    develop = getattr(arguments, "interface", "") or ""
    interface = None if develop else interface_directory()
    if develop:
        logger.info(f"frank: serving the interface from {develop} — changes reload without a build.")
    elif interface is None:
        logger.info(
            "frank: the interface has not been built, so this will serve the control plane but no "
            "screens. Run `cd web && bun run build` in a checkout, or install the packaged build."
        )
    application = build_application(
        f"http://127.0.0.1:{daemon_port}", daemon_token, interface, interface_url=develop,
    )
    guarded = require_token(application, payload["token"])

    # Put it on the tailnet before saying it is available, so that a failure here is reported
    # instead of a QR code somebody would scan and wait on.
    try:
        ensure_served(arguments.port)
    except TailscaleUnavailable as error:
        _report(error)
        return 1

    _describe(payload, getattr(arguments, "image", "") or "")
    logger.info(f"Serving on {payload['endpoint']}. Scan the code with Frank on your phone, or paste the link.")

    # No TLS here. Tailscale terminates it, with a certificate issued for this machine's tailnet
    # name — which is the one thing this process could not obtain for itself and the reason there
    # is no `--tls-certificate` any more.
    configuration = uvicorn.Config(
        guarded, host=host, port=arguments.port, log_level="warning",
    )
    uvicorn.Server(configuration).run()
    return 0
