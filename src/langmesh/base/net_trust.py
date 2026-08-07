"""The shared outbound-request trust guard, for any URL a peer influenced."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UntrustedHostError(Exception):
    """A URL's host is malformed, unresolvable, or private when the caller did not opt into that."""


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
    """Every address `host` resolves to, raising when it cannot be resolved."""
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
    """Raise unless `host` resolves entirely to public addresses, since every one must pass."""
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


def assert_public_url(
    url: str, *, allow_private: bool = False, schemes: frozenset[str] = frozenset({"http", "https"})
) -> None:
    """Raise unless `url` is an http URL whose host resolves to public addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in schemes:
        raise UntrustedHostError(f"unsupported scheme {parsed.scheme!r} in {url!r}")
    assert_public_host(parsed.hostname or "", allow_private=allow_private)


def resolve_public_ips(url: str, *, allow_private: bool = False) -> tuple[str, list[str]]:
    """Assert the host resolves entirely to public addresses, and return them for pinning."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UntrustedHostError(f"unsupported scheme {parsed.scheme!r} in {url!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise UntrustedHostError("missing host")
    if host in _LOOPBACK_NAMES:
        if not allow_private:
            raise UntrustedHostError(f"host {host!r} is loopback; set allow_private to permit it")
        return host, ["127.0.0.1"]
    addresses = _resolved_addresses(host)
    for address in addresses:
        if _is_blocked(address) and not allow_private:
            raise UntrustedHostError(f"host {host!r} resolves to non-public address {address}")
    return host, [str(address) for address in addresses]


def pin_to_ip(url: str, ip: str, hostname: str) -> tuple[str, dict, dict]:
    """Rewrite a URL to connect to a verified address while keeping the real hostname for routing and TLS."""
    parsed = urlparse(url)
    netloc_ip = f"[{ip}]" if ":" in ip else ip
    if parsed.port:
        netloc_ip += f":{parsed.port}"
    pinned_url = parsed._replace(netloc=netloc_ip).geturl()
    headers = {"Host": parsed.netloc}
    extensions = {"sni_hostname": hostname} if parsed.scheme == "https" else {}
    return pinned_url, headers, extensions
