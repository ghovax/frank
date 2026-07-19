"""Shared outbound-request trust guard (anti-SSRF).

Any place the harness fetches a URL a peer influenced — an inbound ``FileWithUri``, a
client-registered push-notification webhook, a remote agent's advertised card URL — must
refuse a target that resolves into a private/loopback/link-local range, or the peer can
make the server reach internal services (cloud metadata, an intranet host) on its behalf.

The check resolves the hostname and inspects the *resolved IP addresses*, not the hostname
string — a DNS name pointing at ``169.254.169.254`` is exactly the bypass a string check
misses. IP literals are checked directly. Callers that legitimately need a private target
(a Daisy-to-Daisy loopback test) opt in explicitly with ``allow_private``.

Residual: a name that passes the check and then rebinds to a private IP before the socket
connects (DNS rebinding) is not fully prevented here — pinning the connection to the
resolved IP is incompatible with the environment's forward proxy. Resolving and rejecting
*every* returned address shrinks the window to a same-request rebind, and the origin check
in the remote-agent path is an independent second guard.
"""

import ipaddress
import socket
from urllib.parse import urlparse


class UntrustedHostError(Exception):
    """A URL's host is malformed, unresolvable, or resolves into a private/loopback range
    the caller did not opt into."""


_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def _is_blocked(address: ipaddress._BaseAddress) -> bool:
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _resolved_addresses(host: str) -> list[ipaddress._BaseAddress]:
    """Every IP ``host`` resolves to (the literal itself when it is already an IP).
    Raises :class:`UntrustedHostError` if it cannot be resolved."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exception:
        raise UntrustedHostError(f"host {host!r} does not resolve: {exception}") from exception
    addresses = []
    for *_, sockaddr in infos:
        try:
            addresses.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    if not addresses:
        raise UntrustedHostError(f"host {host!r} resolved to no usable address")
    return addresses


def assert_public_host(host: str, *, allow_private: bool = False) -> None:
    """Raise :class:`UntrustedHostError` unless ``host`` resolves entirely to public
    addresses (or ``allow_private`` is set). Every resolved address must pass — a name that
    resolves to one public and one loopback address is rejected."""
    host = (host or "").lower()
    if not host:
        raise UntrustedHostError("missing host")
    if host in _LOOPBACK_NAMES:
        if not allow_private:
            raise UntrustedHostError(f"host {host!r} is loopback; set allow_private to permit it")
        return
    for address in _resolved_addresses(host):
        if _is_blocked(address) and not allow_private:
            raise UntrustedHostError(
                f"host {host!r} resolves to non-public address {address}; set allow_private to permit it"
            )


def assert_public_url(url: str, *, allow_private: bool = False, schemes: frozenset[str] = frozenset({"http", "https"})) -> None:
    """Raise :class:`UntrustedHostError` unless ``url`` is an ``http(s)`` URL whose host
    resolves to public addresses (or ``allow_private`` is set)."""
    parsed = urlparse(url)
    if parsed.scheme not in schemes:
        raise UntrustedHostError(f"unsupported scheme {parsed.scheme!r} in {url!r}")
    assert_public_host(parsed.hostname or "", allow_private=allow_private)
