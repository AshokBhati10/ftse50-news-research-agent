"""SSRF guards for outbound URL fetches triggered by model tool calls.

The model chooses fetch_article targets, so every outbound fetch must be
restricted to public HTTP(S) resources. Two layers:

- :func:`assert_public_url` — synchronous, syntax-level checks (scheme, port,
  credentials, localhost/private/reserved IP literals).
- :func:`assert_public_url_resolved` — asynchronous DNS resolution check, so a
  public-looking hostname that resolves to a private/loopback/link-local
  address is also blocked.

Fail closed: anything uncertain (unparseable, unresolvable) is treated as
unsafe. Residual limitation: DNS rebinding between the check and the actual
fetch is not mitigated; the fetch itself must not be given custom resolvers.
"""

from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import urlparse

_ALLOWED_SCHEMES = ("http", "https")
_ALLOWED_PORTS = {None, 80, 443}
_FORBIDDEN_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain")


class UnsafeURLError(ValueError):
    """The URL targets a non-public resource and must not be fetched."""


def _assert_ip_public(ip: ipaddress._BaseAddress) -> None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_unspecified or ip.is_reserved):
        raise UnsafeURLError(f"IP address {ip} is not a public address")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def assert_public_url(url: str) -> str:
    """Syntax-level SSRF check. Raises :class:`UnsafeURLError` when unsafe."""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise UnsafeURLError(f"URL could not be parsed: {exc}") from exc
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme {parsed.scheme!r} is not http(s)")
    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsafeURLError("URL has no host")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURLError("URL must not embed credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURLError(f"URL port invalid: {exc}") from exc
    if port not in _ALLOWED_PORTS:
        raise UnsafeURLError(f"port {port} is not an allowed public web port")
    if host == "localhost" or host.endswith(_FORBIDDEN_HOST_SUFFIXES):
        raise UnsafeURLError(f"host {host!r} is not a public host")
    if _is_ip_literal(host):
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return url
        _assert_ip_public(ip)
    return url


async def assert_public_url_resolved(url: str) -> str:
    """DNS-level SSRF check: every resolved address must be public."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsafeURLError("URL has no host")
    if _is_ip_literal(host):
        return assert_public_url(url)
    assert_public_url(url)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except Exception as exc:  # noqa: BLE001 — fail closed on DNS trouble
        raise UnsafeURLError(f"host {host!r} could not be resolved: {exc}") from exc
    for sockaddr in {info[4] for info in infos}:
        try:
            ip = ipaddress.ip_address(str(sockaddr[0]))
        except ValueError:
            continue
        _assert_ip_public(ip)
    return url