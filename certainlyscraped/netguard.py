"""SSRF guard: refuse URLs that point at private, loopback or link-local
addresses before any request is made.

`check_url` is deliberately conservative: every address a host resolves to is
checked, so a name with one public and one RFC 1918 answer is refused. Legacy
IPv4 spellings (`0x7f.1`, `2130706433`, `0177.0.0.1`, `127.1`) are parsed with
`socket.inet_aton` so they cannot slip past `ipaddress`.

Residual risk: DNS rebinding. The name is resolved here and resolved again by
the HTTP client at connect time; an attacker-controlled DNS server can answer
public first and private second. Closing that gap needs the socket itself to
be pinned to the vetted address (or a network-level egress policy), which the
plain httpx/curl_cffi clients cannot express. Treat this as one layer, not the
only one: the MCP server should also run without ambient credentials.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class PrivateAddressError(ValueError):
    """The URL resolves to (or literally names) a non-public address."""


def check_url(url: str, allow_private: bool = False,
              resolve=socket.getaddrinfo) -> None:
    """Raise unless `url` is http(s) and every target address is public.

    allow_private=True keeps the scheme check but skips address checks; that
    is for an explicit local-only mode, never the default.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        # Never echo the URL: it may embed credentials.
        raise PrivateAddressError("private address: unparseable URL") from None

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise PrivateAddressError(f"scheme not allowed: {scheme}")

    if allow_private:
        return

    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise PrivateAddressError("private address: invalid host or port") from None
    if not host:
        raise PrivateAddressError("private address: URL has no host")
    host = host.rstrip(".").lower() if host.endswith(".") else host.lower()
    # Keep the original spelling for messages (localhost. must name itself).
    display = parts.hostname or host

    if host == "localhost" or host.endswith(".localhost"):
        raise PrivateAddressError(f"private address: {display}")

    literal = _literal_ip(host)
    if literal is not None:
        if _non_public(literal):
            raise PrivateAddressError(f"private address: {display}")
        return

    target_port = port or (443 if scheme == "https" else 80)
    try:
        infos = resolve(host, target_port, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as e:
        # Not a private-address finding: the caller may treat this as a
        # plain fetch failure and try another tier.
        raise ValueError(f"cannot resolve {host}") from e

    addresses = []
    for info in infos:
        sockaddr = info[4]
        raw = str(sockaddr[0]).split("%", 1)[0]  # drop IPv6 zone id
        try:
            addresses.append(ipaddress.ip_address(raw))
        except ValueError:
            raise ValueError(f"cannot resolve {host}") from None
    if not addresses:
        raise ValueError(f"cannot resolve {host}")

    if any(_non_public(ip) for ip in addresses):
        raise PrivateAddressError(f"private address: {display}")


def _literal_ip(host: str) -> _IPAddress | None:
    """Parse `host` as an IP, including the legacy inet_aton spellings."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def _non_public(ip: _IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        # An IPv4 address smuggled in as IPv6 must pass the same test.
        if ip.ipv4_mapped is not None and _non_public(ip.ipv4_mapped):
            return True
        if ip.sixtofour is not None and _non_public(ip.sixtofour):
            return True
        if ip.teredo is not None and any(_non_public(part) for part in ip.teredo):
            return True
    elif not ip.is_global:
        # IPv4 gets one extra test: shared/CGNAT space (100.64.0.0/10) is not
        # `is_private` on every supported Python and is not globally routable
        # either -- and is exactly where a Tailscale/WireGuard peer lives.
        return True
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_unspecified or ip.is_reserved or ip.is_multicast)
