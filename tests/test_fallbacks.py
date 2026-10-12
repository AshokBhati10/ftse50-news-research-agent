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
        return None, {}

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
        return "Fallback extracted body text. " * 30, {}

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

    from nodes import runner as runner_mod
    from nodes.deduplication import SemanticDeduper

    settings = _settings()
    settings.top_n_articles = 3
    company = Company(name="AstraZeneca", ticker="AZN.L", market_cap=1e11)

    arts = [_article()]
    arts[0].extraction_status = "mock"
    arts[0].content = "Body text. " * 20
    deduper = SemanticDeduper(embed_fn=lambda texts: np.ones((len(texts), 2)))

    report = await runner_mod.run_company_pipeline(
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
    from nodes import runner as runner_mod
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
    report = await runner_mod.run_company_pipeline(
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


# ---------------------------------------------------------------------------
# SSRF guards: private targets blocked; redirects validated per hop
# ---------------------------------------------------------------------------

async def test_extract_article_blocks_private_url(monkeypatch):
    """A search result pointing at a private address must fail the article
    (never the run) and must never be fetched."""
    fetched = {"n": 0}

    async def boom_crawl(url, settings):
        fetched["n"] += 1
        raise AssertionError("crawl4ai must not run for private URLs")

    monkeypatch.setattr(crawl_mod, "_crawl4ai_extract", boom_crawl)

    article = Article(
        company="X", ticker="", title="t",
        url="http://169.254.169.254/latest/meta-data/",
        snippet="s", search_provider="searxng")
    out = await crawl_mod.extract_article(article, _settings(), cache=None)
    assert out.extraction_status == "failed"
    assert fetched["n"] == 0


async def test_fallback_fetch_follows_public_redirect_only(monkeypatch):
    """Redirects are followed manually and validated hop by hop: public hops
    succeed, redirects to loopback/private targets are never fetched."""
    import httpx

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/public-redirect":
            return httpx.Response(
                302, headers={"Location": "http://final.example.com/article"})
        if request.url.path == "/private-redirect":
            return httpx.Response(
                302, headers={"Location": "http://127.0.0.1:9100/secret"})
        if request.url.path == "/article":
            body = ("<html><body><article><p>%s</p></article></body></html>"
                    % ("Enough readable body text for extraction. " * 30))
            return httpx.Response(200, text=body,
                                  headers={"content-type": "text/html"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(crawl_mod.httpx, "AsyncClient", factory)

    ok, _meta = await crawl_mod._fallback_fetch(
        "http://start.example.com/public-redirect", _settings())
    assert ok is not None
    assert "Enough readable body text" in ok

    blocked, _bmeta = await crawl_mod._fallback_fetch(
        "http://start.example.com/private-redirect", _settings())
    assert blocked is None
    assert not any(":9100" in u for u in seen)  # private hop never fetched
    assert any(u.endswith("/article") for u in seen)  # public hop was fetched
