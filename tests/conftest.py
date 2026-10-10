"""Shared test fixtures: keep DNS-level SSRF checks offline.

The production path resolves hostnames to reject DNS-based private-address
SSRF. In tests that would hit real DNS for example.com-style URLs, so the
check is replaced by the (network-free) syntax-level validation. The DNS
behaviour itself is covered in :mod:`tests.test_net_guard` with a mocked
resolver.
"""

from __future__ import annotations

import pytest

from utils import net_guard


@pytest.fixture(autouse=True)
def _offline_dns_guard(monkeypatch):
    async def _syntax_only(url: str) -> str:
        return net_guard.assert_public_url(url)

    from nodes import crawl as crawl_mod

    monkeypatch.setattr(crawl_mod, "assert_public_url_resolved", _syntax_only)
    return _syntax_only