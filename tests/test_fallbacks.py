"""Tests for failure/fallback behaviour across the pipeline."""

import pytest

from config.settings import Settings
from nodes import crawl as crawl_mod
from nodes.analysis import AnalysisError
from utils.models import Article, Company


def _article() -> Article:
    return Article(
        company="AstraZeneca", ticker="AZN.L",
        title="Some news", url="https://example.com/article",
        snippet="A short snippet with enough context to be useful.",
        search_provider="searxng",
    )


def _settings() -> Settings:
    s = Settings()
    s.crawl_max_retries = 0
    return s


@pytest.mark.asyncio
async def test_crawl_failure_falls_back_to_snippet(monkeypatch):
    """Crawl4AI down + fallback extractor empty -> snippet_only, no crash."""

    async def boom_crawl(url, settings):
        raise RuntimeError("browser exploded")

    async def empty_fallback(url, settings):
        return None

    monkeypatch.setattr(crawl_mod, "_crawl4ai_extract", boom_crawl)
    monkeypatch.setattr(crawl_mod, "_fallback_fetch", empty_fallback)

    article = await crawl_mod.extract_article(_article(), _settings(), cache=None)
    assert article.extraction_status == "snippet_only"
    assert article.evidence_excerpt != ""


@pytest.mark.asyncio
async def test_crawl_failure_uses_fallback_extractor(monkeypatch):
    async def boom_crawl(url, settings):
        raise RuntimeError("browser exploded")

    async def fake_fallback(url, settings):
        return "Fallback extracted body text. " * 30

    monkeypatch.setattr(crawl_mod, "_crawl4ai_extract", boom_crawl)
    monkeypatch.setattr(crawl_mod, "_fallback_fetch", fake_fallback)

    article = await crawl_mod.extract_article(_article(), _settings(), cache=None)
    assert article.extraction_status == "ok"
    assert "Fallback extracted" in article.content


@pytest.mark.asyncio
async def test_fallback_extract_parses_article_html():
    html = """
    <html><body><nav>menu</nav><article>
    <p>%s</p><p>%s</p><p>%s</p>
    </article></body></html>
    """ % ("First paragraph with plenty of words. " * 10,
           "Second paragraph with plenty of words. " * 10,
           "Third paragraph with plenty of words. " * 10)
    text = crawl_mod.fallback_extract("https://example.com/x", html, min_chars=100)
    assert text is not None
    assert "First paragraph" in text
    assert "menu" not in text


@pytest.mark.asyncio
async def test_mock_pipeline_end_to_end():
    """Mock path: articles in -> heuristic analysis -> ranked report, no raise."""
    import asyncio

    import numpy as np

    import main as main_mod
    from nodes.deduplication import SemanticDeduper

    settings = _settings()
    settings.top_n_articles = 3
    company = Company(name="AstraZeneca", ticker="AZN.L", market_cap=1e11)

    arts = [_article()]
    arts[0].extraction_status = "mock"
    arts[0].content = "Body text. " * 20
    deduper = SemanticDeduper(embed_fn=lambda texts: np.ones((len(texts), 2)))

    report = await main_mod.run_company_pipeline(
        company, settings, cache=None, deduper=deduper,
        search_sem=asyncio.Semaphore(1), llm_sem=asyncio.Semaphore(1),
        mock_articles=arts,
    )
    assert report.ticker == "AZN.L"
    assert len(report.articles) == 1
    assert report.providers_used == ["mock"]


@pytest.mark.asyncio
async def test_live_company_failure_recorded_and_continues(monkeypatch):
    """Live path: analyze_company raises -> CompanyReport with notes, no raise."""
    import asyncio
    import main as main_mod
    from nodes import analysis as analysis_mod
    from nodes import search as search_node

    async def boom_analyze(company, articles, settings, cache=None, agent=None):
        raise AnalysisError("LLM down")

    async def fake_search(company, settings, cache=None, max_results=None):
        a = _article()
        a.source_quality = 0.8
        return search_node.SearchOutcome(
            company=company.name, ticker=company.ticker, articles=[a],
            providers_used=["searxng"], fallback_used=False, notes="",
        )

    async def fake_extract(articles, settings, cache=None):
        for a in articles:
            a.extraction_status = "ok"
            a.content = "Body text. " * 50
        return articles

    monkeypatch.setattr(analysis_mod, "analyze_company", boom_analyze)
    monkeypatch.setattr(search_node, "search_company_news", fake_search)
    monkeypatch.setattr("nodes.crawl.extract_articles", fake_extract)

    import numpy as np
    from nodes.deduplication import SemanticDeduper
    deduper = SemanticDeduper(embed_fn=lambda texts: np.ones((len(texts), 2)))

    settings = _settings()
    company = Company(name="AstraZeneca", ticker="AZN.L", market_cap=1e11)
    report = await main_mod.run_company_pipeline(
        company, settings, cache=None, deduper=deduper,
        search_sem=asyncio.Semaphore(1), llm_sem=asyncio.Semaphore(1),
    )
    assert report.ticker == "AZN.L"
    assert report.articles == []
    assert "LLM down" in report.notes


def test_fallback_extract_ignores_navigation_and_advertisements():
    html = """
    <html><body>
      <nav><p>Unrelated navigation content that should never appear.</p></nav>
      <div class="advertisement">
        <p>Buy this stock now for massive gains. Advertisement content.</p>
      </div>
      <article>
        <p>AstraZeneca announced new clinical trial results today. %s</p>
        <p>The company reported additional details about the study. %s</p>
      </article>
    </body></html>
    """ % (
        "The clinical trial data showed meaningful progress. " * 10,
        "Management will discuss the results with investors. " * 10,
    )

    text = crawl_mod.fallback_extract(
        "https://example.com/article", html, min_chars=100
    )

    assert text is not None
    assert "AstraZeneca announced new clinical trial results" in text
    assert "Unrelated navigation content" not in text
    assert "Buy this stock now" not in text
