"""Phase 2.2 — graceful paywall and failed-extraction handling.

Paywalled/registration-walled pages must not crash the run: they are recorded
with a distinct status, their search snippet is kept as evidence, and the
pipeline continues.
"""

from __future__ import annotations

import pytest

from agent.tools import ArticleExtractor, FetchArticleArgs
from config.settings import Settings
from nodes import crawl as crawl_mod
from nodes.runner import execute_query_research
from nodes.search import TopicSearchOutcome
from utils.cache import CacheManager
from utils.models import Article


def _settings(tmp_path) -> Settings:
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    return Settings(cache_dir=cache, output_dir=tmp_path / "out",
                    ollama_api_key="test-key", crawl_max_retries=0)


def _article(**kw) -> Article:
    data = dict(company="X", ticker="X.L", title="", url="https://example.com/a",
                snippet="search snippet", search_provider="searxng")
    data.update(kw)
    return Article(**data)


# ---------------------------------------------------------------------------
# detect_paywall
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Please subscribe to continue reading this article.", True),
    ("Sign in to read the full story", True),
    ("Become a member to unlock premium content", True),
    ("AstraZeneca reported strong quarterly results today.", False),
    ("The company announced a new partnership.", False),
    ("", False),
    # Precision: ordinary nav / unrelated "premium"/"paywall" prose must NOT
    # trigger (the exact classes that a naive keyword match would mislabel).
    ("Already a subscriber? Sign in.", False),
    ("Sign in to read our privacy policy.", False),
    ("Register to read the full free report", False),
    ("The premium content team at Reuters reported on it.", False),
    ("The publisher removed its paywall last year.", False),
    ("Subscribe to read the full free report", False),
    ("Read our premium content policy before subscribing", False),
])
def test_detect_paywall(text, expected):
    assert crawl_mod.detect_paywall(text) is expected


def test_paywall_detection_ignores_script_text():
    html = ('<html><body><p>A perfectly normal article body.</p>'
            '<script>var wall = "subscribe to continue reading";</script>'
            '</body></html>')
    assert crawl_mod.extract_metadata(html)["paywalled"] is False


# ---------------------------------------------------------------------------
# extract_article: paywall is recorded and snippet retained
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_paywall_from_thin_crawl_records_status_and_keeps_snippet(
        tmp_path, monkeypatch):
    async def thin_crawl(url, settings):
        return None, {"paywalled": True, "title": "Premium story"}

    async def empty_fallback(url, settings):
        return None, {}

    monkeypatch.setattr(crawl_mod, "_crawl4ai_extract", thin_crawl)
    monkeypatch.setattr(crawl_mod, "_fallback_fetch", empty_fallback)

    article = await crawl_mod.extract_article(_article(), _settings(tmp_path),
                                              cache=None)
    assert article.extraction_status == "paywalled"
    assert article.content is None
    assert article.evidence_excerpt == "search snippet"
    assert article.title == "Premium story"  # metadata still applied


@pytest.mark.asyncio
async def test_paywall_detected_via_fallback_marker(tmp_path, monkeypatch):
    async def boom_crawl(url, settings):
        raise RuntimeError("browser exploded")

    async def paywalled_fallback(url, settings):
        return None, {"paywalled": True}

    monkeypatch.setattr(crawl_mod, "_crawl4ai_extract", boom_crawl)
    monkeypatch.setattr(crawl_mod, "_fallback_fetch", paywalled_fallback)

    article = await crawl_mod.extract_article(_article(), _settings(tmp_path),
                                              cache=None)
    assert article.extraction_status == "paywalled"
    assert article.evidence_excerpt == "search snippet"


@pytest.mark.asyncio
async def test_paywall_without_snippet_is_paywalled_not_failed(tmp_path,
                                                               monkeypatch):
    async def thin_crawl(url, settings):
        return None, {"paywalled": True}

    async def empty_fallback(url, settings):
        return None, {}

    monkeypatch.setattr(crawl_mod, "_crawl4ai_extract", thin_crawl)
    monkeypatch.setattr(crawl_mod, "_fallback_fetch", empty_fallback)
    article = await crawl_mod.extract_article(
        _article(snippet=""), _settings(tmp_path), cache=None)
    assert article.extraction_status == "paywalled"


@pytest.mark.asyncio
async def test_plain_failure_still_snippet_only(tmp_path, monkeypatch):
    async def thin_crawl(url, settings):
        return None, {"paywalled": False}

    async def empty_fallback(url, settings):
        return None, {}

    monkeypatch.setattr(crawl_mod, "_crawl4ai_extract", thin_crawl)
    monkeypatch.setattr(crawl_mod, "_fallback_fetch", empty_fallback)
    article = await crawl_mod.extract_article(_article(), _settings(tmp_path),
                                              cache=None)
    assert article.extraction_status == "snippet_only"


# ---------------------------------------------------------------------------
# ArticleExtractor (agent fetch tool)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_article_extractor_reports_paywall(tmp_path, monkeypatch):
    async def thin_crawl(url, settings):
        return None, {"paywalled": True}

    async def empty_fallback(url, settings):
        return None, {}

    monkeypatch.setattr(crawl_mod, "_crawl4ai_extract", thin_crawl)
    monkeypatch.setattr(crawl_mod, "_fallback_fetch", empty_fallback)
    extractor = ArticleExtractor(_settings(tmp_path))
    obs = await extractor.run(FetchArticleArgs(url="https://example.com/a"))
    assert obs["ok"] is False
    assert obs["extraction_status"] == "paywalled"
    assert "paywalled" in (obs["error"] or "").lower()


# ---------------------------------------------------------------------------
# Non-agentic pipeline: paywalled items still analysed via snippet + warning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_paywalled_articles_analysed_via_snippet_with_warning(
        tmp_path, monkeypatch):
    async def fake_search(query, settings, cache, *, max_results=20,
                          time_range="month", fresh=False):
        return TopicSearchOutcome(query=query, articles=[_article()],
                                  providers_used=["searxng"],
                                  time_range=time_range)

    async def fake_extract(articles, settings, cache=None):
        for a in articles:
            a.extraction_status = "paywalled"
            a.content = None
            a.snippet = "snippet body"
        return articles

    class FakeDeduper:
        async def deduplicate_async(self, articles):
            return articles

    seen = {}

    async def fake_analyze(query, articles, settings, cache=None, agent=None,
                           top_n=None):
        seen["articles"] = list(articles)
        return []

    monkeypatch.setattr("nodes.search.search_topic", fake_search)
    monkeypatch.setattr("nodes.crawl.extract_articles", fake_extract)
    monkeypatch.setattr("nodes.deduplication.build_deduper",
                        lambda s: FakeDeduper())
    monkeypatch.setattr("nodes.analysis.analyze_query", fake_analyze)

    settings = _settings(tmp_path)
    report = await execute_query_research(
        "q", settings, CacheManager(settings.cache_dir), run_id="r",
        agentic=False)

    assert any("paywalled" in w.lower() for w in report.warnings)
    # The paywalled article reached the analyser using its snippet.
    assert seen["articles"]
    assert seen["articles"][0].text_for_analysis().startswith("snippet")
