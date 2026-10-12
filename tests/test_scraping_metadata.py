"""Phase 2.1 — article metadata & publication-date extraction.

`extract_metadata` is pure and offline; `extract_article`'s metadata plumbing is
tested with the crawler stubbed.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from config.settings import Settings
from nodes import crawl as crawl_mod
from utils.cache import CacheManager
from utils.models import Article


def _settings(tmp_path) -> Settings:
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    return Settings(cache_dir=cache, output_dir=tmp_path / "out",
                    crawl_max_retries=0)


def _article(url="https://example.com/a", **kw) -> Article:
    data = dict(company="X", ticker="X.L", title="", url=url,
                snippet="search snippet", search_provider="searxng")
    data.update(kw)
    return Article(**data)


# ---------------------------------------------------------------------------
# extract_metadata
# ---------------------------------------------------------------------------

def test_extract_metadata_jsonld():
    html = """
    <html><head>
      <script type="application/ld+json">
      {"@type":"NewsArticle","headline":"AstraZeneca in $2bn deal",
       "datePublished":"2026-10-05T09:30:00Z",
       "publisher":{"@type":"Organization","name":"Reuters"}}
      </script>
    </head><body>...</body></html>
    """
    meta = crawl_mod.extract_metadata(html, "https://reuters.com/x")
    assert meta["title"] == "AstraZeneca in $2bn deal"
    assert meta["source"] == "Reuters"
    assert meta["published_at"] == datetime(2026, 10, 5, 9, 30,
                                            tzinfo=timezone.utc)


def test_extract_metadata_og_and_meta_tags():
    html = """
    <html><head>
      <meta property="article:published_time" content="2026-09-30T12:00:00+00:00">
      <meta property="og:site_name" content="Financial Times">
      <title>FT story</title>
    </head><body></body></html>
    """
    meta = crawl_mod.extract_metadata(html)
    assert meta["published_at"].date() == datetime(2026, 9, 30).date()
    assert meta["source"] == "Financial Times"
    assert meta["title"] == "FT story"


def test_extract_metadata_time_element_fallback():
    html = ('<html><body><article>'
            '<time datetime="2026-10-01T08:00:00Z">1 Oct 2026</time>'
            '<p>body</p></article></body></html>')
    meta = crawl_mod.extract_metadata(html)
    assert meta["published_at"] == datetime(2026, 10, 1, 8, 0,
                                            tzinfo=timezone.utc)


def test_extract_metadata_no_date_is_none_and_never_raises():
    assert crawl_mod.extract_metadata("<html><body>x</body></html>")["published_at"] is None
    assert crawl_mod.extract_metadata("not html at all")["published_at"] is None
    assert crawl_mod.extract_metadata("")["published_at"] is None


def test_extract_metadata_jsonld_prefers_article_node_over_wrapper():
    html = """
    <html><head><script type="application/ld+json">
    {"@graph":[
      {"@type":"WebPage","name":"Home page",
       "dateModified":"2026-09-09T00:00:00Z"},
      {"@type":"NewsArticle","headline":"Real article headline",
       "datePublished":"2026-03-03T00:00:00Z"}
    ]}</script></head><body></body></html>
    """
    meta = crawl_mod.extract_metadata(html)
    assert meta["title"] == "Real article headline"
    assert meta["published_at"] == datetime(2026, 3, 3, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# extract_article metadata plumbing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_article_applies_crawl_metadata(tmp_path, monkeypatch):
    async def fake_crawl(url, settings):
        return "Full article body text. " * 30, {
            "title": "Real headline", "source": "Reuters",
            "published_at": datetime(2026, 10, 4, 7, 0, tzinfo=timezone.utc)}

    monkeypatch.setattr(crawl_mod, "_crawl4ai_extract", fake_crawl)
    article = await crawl_mod.extract_article(_article(), _settings(tmp_path),
                                              cache=None)
    assert article.extraction_status == "ok"
    assert article.title == "Real headline"
    assert article.source == "Reuters"
    assert article.published_at == datetime(2026, 10, 4, 7, 0,
                                            tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_extract_article_does_not_overwrite_existing_metadata(
        tmp_path, monkeypatch):
    async def fake_crawl(url, settings):
        return "Full article body text. " * 30, {
            "title": "Page title", "source": "Page source",
            "published_at": datetime(2026, 10, 4, tzinfo=timezone.utc)}

    monkeypatch.setattr(crawl_mod, "_crawl4ai_extract", fake_crawl)
    existing = datetime(2026, 9, 1, tzinfo=timezone.utc)
    article = _article(title="Search title", source="search.example",
                       published_at=existing)
    out = await crawl_mod.extract_article(article, _settings(tmp_path),
                                          cache=None)
    assert out.title == "Search title"          # not overwritten
    assert out.source == "search.example"        # not overwritten
    assert out.published_at == existing          # not overwritten


@pytest.mark.asyncio
async def test_extract_article_cache_restores_metadata(tmp_path, monkeypatch):
    calls = {"n": 0}

    async def fake_crawl(url, settings):
        calls["n"] += 1
        return "Full article body text. " * 30, {
            "title": "Cached title", "source": "Reuters",
            "published_at": datetime(2026, 10, 4, tzinfo=timezone.utc)}

    monkeypatch.setattr(crawl_mod, "_crawl4ai_extract", fake_crawl)
    settings = _settings(tmp_path)
    cache = CacheManager(settings.cache_dir)

    first = await crawl_mod.extract_article(_article(), settings, cache=cache)
    assert first.published_at is not None
    # Second run hits the cache: no new crawl, metadata restored.
    second = await crawl_mod.extract_article(_article(), settings, cache=cache)
    assert calls["n"] == 1
    assert second.extraction_status == "ok"
    assert second.title == "Cached title"
    assert second.source == "Reuters"
    assert second.published_at == datetime(2026, 10, 4, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_cache_hit_snippet_only_restores_evidence(tmp_path, monkeypatch):
    async def thin_crawl(url, settings):
        return None, {"paywalled": False}

    async def empty_fallback(url, settings):
        return None, {}

    monkeypatch.setattr(crawl_mod, "_crawl4ai_extract", thin_crawl)
    monkeypatch.setattr(crawl_mod, "_fallback_fetch", empty_fallback)
    settings = _settings(tmp_path)
    cache = CacheManager(settings.cache_dir)

    first = await crawl_mod.extract_article(_article(), settings, cache=cache)
    assert first.extraction_status == "snippet_only"
    # Cached snippet-only entry must restore its snippet as the evidence.
    second = await crawl_mod.extract_article(_article(), settings, cache=cache)
    assert second.extraction_status == "snippet_only"
    assert second.evidence_excerpt == "search snippet"


@pytest.mark.asyncio
async def test_extract_article_fallback_carries_metadata(tmp_path, monkeypatch):
    async def boom_crawl(url, settings):
        raise RuntimeError("browser exploded")

    async def fallback(url, settings):
        return "Fallback body text. " * 30, {
            "title": "Fallback headline", "source": "example.com",
            "published_at": datetime(2026, 10, 3, tzinfo=timezone.utc)}

    monkeypatch.setattr(crawl_mod, "_crawl4ai_extract", boom_crawl)
    monkeypatch.setattr(crawl_mod, "_fallback_fetch", fallback)
    article = await crawl_mod.extract_article(_article(), _settings(tmp_path),
                                              cache=None)
    assert article.extraction_status == "ok"
    assert article.title == "Fallback headline"
    assert article.published_at == datetime(2026, 10, 3, tzinfo=timezone.utc)
