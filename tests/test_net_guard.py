"""Tests for the SSRF URL guard used by fetch_article and the crawler.

The DNS resolution layer is mocked; the guard's logic is what is under test.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from utils.net_guard import (
    UnsafeURLError,
    assert_public_url,
    assert_public_url_resolved,
)


@pytest.mark.parametrize("url", [
    "http://localhost:8000/admin",
    "http://LOCALHOST/x",
    "http://127.0.0.1:9000/secret",
    "http://0.0.0.0/x",
    "http://[::1]/x",
    "http://[::ffff:127.0.0.1]/x",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.1.2.3/x",
    "http://172.16.0.9/x",
    "http://192.168.1.5/x",
    "http://[fe80::1]/x",
    "http://user:pass@10.0.0.1/x",
    "http://metadata.internal/.aws/credentials",
    "http://myhost.local/x",
    "https://example.com:8443/x",
    "file:///etc/passwd",
    "gopher://example.com",
])
def test_assert_public_url_blocks_unsafe(url):
    with pytest.raises(UnsafeURLError):
        assert_public_url(url)


@pytest.mark.parametrize("url", [
    "https://example.com/news/astrazeneca",
    "http://www.fool.co.uk/article",
    "https://www.astrazeneca.com/media-centre/press-releases.html",
])
def test_assert_public_url_allows_public(url):
    assert assert_public_url(url) == url


class _FakeLoop:
    def __init__(self, infos, error=None):
        self._infos = infos
        self._error = error

    async def getaddrinfo(self, host, port, *args, **kwargs):
        if self._error is not None:
            raise self._error
        return self._infos


def _info(ip: str, port: int = 80) -> tuple:
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))


async def test_resolved_hostname_to_private_ip_blocked(monkeypatch):
    monkeypatch.setattr(asyncio, "get_running_loop",
                        lambda: _FakeLoop([_info("192.168.1.10")]))
    with pytest.raises(UnsafeURLError):
        await assert_public_url_resolved("https://rebind.example.com/x")


async def test_resolved_hostname_to_public_ip_allowed(monkeypatch):
    monkeypatch.setattr(asyncio, "get_running_loop",
                        lambda: _FakeLoop([_info("93.184.216.34", 443)]))
    assert await assert_public_url_resolved("https://example.com/x") == \
        "https://example.com/x"


async def test_resolution_failure_fails_closed(monkeypatch):
    monkeypatch.setattr(asyncio, "get_running_loop",
                        lambda: _FakeLoop([], error=OSError("no dns")))
    with pytest.raises(UnsafeURLError):
        await assert_public_url_resolved("https://unresolvable.example/x")


async def test_multi_record_all_addresses_must_be_public(monkeypatch):
    infos = [_info("93.184.216.34", 443), _info("127.0.0.1", 443)]
    monkeypatch.setattr(asyncio, "get_running_loop",
                        lambda: _FakeLoop(infos))
    with pytest.raises(UnsafeURLError):
        await assert_public_url_resolved("https://mixed.example.com/x")