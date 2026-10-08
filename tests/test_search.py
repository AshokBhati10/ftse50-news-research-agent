"""Tests for nodes/search.py — URL normalisation, merging, fallback cascade."""

import pytest

from config.settings import Settings
from nodes import search as search_node
from nodes.search import (
    SearchError,
    dedup_by_url,
    normalize_url,
    search_company_news,
)
from utils.models import Article, Company


def _article(title: str, url: str, provider: str = "searxng") -> Article:
    return Article(company="AstraZeneca", ticker="AZN.L", title=title, url=url,
                   search_provider=provider)


@pytest.mark.parametrize(
    "a,b",
    [
        # tracking params stripped
        ("https://www.reuters.com/a/b?utm_source=x&x=1", "https://reuters.com/a/b?x=1"),
        # www + trailing slash
        ("https://www.bbc.com/news/x/", "https://bbc.com/news/x"),
        # case-insensitive host
        ("HTTPS://REUTERS.COM/a", "https://reuters.com/a"),
        # fragments dropped
        ("https://ft.com/content/1#section", "https://ft.com/content/1"),
    ],
)
def test_normalize_url_equivalents(a, b):
    assert normalize_url(a) == normalize_url(b)


def test_normalize_url_keeps_distinct_paths():
    assert normalize_url("https://reuters.com/a/1") != normalize_url("https://reuters.com/a/2")


def test_dedup_by_url_removes_tracking_variants():
    arts = [
        _article("t1", "https://reuters.com/x?utm_source=feed"),
        _article("t1 copy", "https://www.reuters.com/x"),
        _article("t2", "https://reuters.com/y"),
    ]
    out = dedup_by_url(arts)
    assert len(out) == 2
    assert out[0].title == "t1"  # first occurrence wins


def test_query_builder_primary_mentions_company_and_ticker():
    from utils.query_builder import build_primary_query
    q = build_primary_query(Company(name="AstraZeneca", ticker="AZN.L"))
    assert "AstraZeneca" in q and "AZN" in q


@pytest.mark.asyncio
async def test_fallback_chain_searxng_ok_no_youcom(monkeypatch):
    """Sufficient SearXNG results -> You.com must NOT be called."""
    settings = Settings()
    company = Company(name="AstraZeneca", ticker="AZN.L")

    async def fake_searxng(query, _settings, max_results=10, time_range="month"):
        return [_article(f"news {i}", f"https://reuters.com/{i}") for i in range(6)]

    async def fake_youcom(*a, **k):
        raise AssertionError("You.com should not be called")

    async def fake_yf(company, max_results=10):
        return []

    monkeypatch.setattr(search_node, "searxng_search", fake_searxng)
    monkeypatch.setattr(search_node, "youcom_search", fake_youcom)
    monkeypatch.setattr(search_node, "yfinance_news", fake_yf)

    outcome = await search_company_news(company, settings, cache=None)
    assert len(outcome.articles) == 6
    assert not outcome.fallback_used
    assert outcome.providers_used == ["searxng"]


@pytest.mark.asyncio
async def test_fallback_chain_searxng_fails_then_youcom(monkeypatch):
    settings = Settings(you_api_key="dummy")
    company = Company(name="AstraZeneca", ticker="AZN.L")

    async def fake_searxng(*a, **k):
        raise SearchError("boom")

    async def fake_youcom(query, _settings, max_results=10):
        return [_article("fb news", "https://you.com/1", provider="you.com")]

    async def fake_yf(company, max_results=10):
        return [_article("yf headline", "https://finance.yahoo.com/1", provider="yfinance")]

    monkeypatch.setattr(search_node, "searxng_search", fake_searxng)
    monkeypatch.setattr(search_node, "youcom_search", fake_youcom)
    monkeypatch.setattr(search_node, "yfinance_news", fake_yf)

    outcome = await search_company_news(company, settings, cache=None)
    assert outcome.fallback_used
    assert set(outcome.providers_used) == {"you.com", "yfinance"}
    assert len(outcome.articles) == 2


@pytest.mark.asyncio
async def test_recency_filter_drops_stale_articles(monkeypatch):
    from datetime import datetime, timedelta, timezone

    settings = Settings(news_lookback_days=14)
    company = Company(name="AstraZeneca", ticker="AZN.L")
    fresh = _article("fresh", "https://reuters.com/fresh")
    fresh.published_at = datetime.now(timezone.utc) - timedelta(days=1)
    stale = _article("stale", "https://reuters.com/stale")
    stale.published_at = datetime.now(timezone.utc) - timedelta(days=60)

    async def fake_searxng(*a, **k):
        return [fresh, stale]

    async def fake_yf(company, max_results=10):
        return []

    monkeypatch.setattr(search_node, "searxng_search", fake_searxng)
    monkeypatch.setattr(search_node, "yfinance_news", fake_yf)

    outcome = await search_company_news(company, settings, cache=None)
    urls = [a.url for a in outcome.articles]
    assert "https://reuters.com/fresh" in urls
    assert "https://reuters.com/stale" not in urls
