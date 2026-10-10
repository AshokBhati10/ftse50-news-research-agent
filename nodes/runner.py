"""Shared research-run orchestration.

This module owns the per-company pipeline loop used by **both** the CLI
(``main.py``) and the FastAPI backend (``api/``), so the two interfaces can
never drift apart. Business logic lives in the ``nodes/`` modules; this file
only wires them together with bounded concurrency.

Progress reporting: pass ``on_company_complete`` to receive each finished
:class:`~utils.models.CompanyReport` (including failed ones) as soon as it
is ready — the API uses this for live SSE updates, the CLI for its progress
bar.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from config.settings import Settings
from utils.cache import CacheManager
from utils.models import Article, Company, CompanyReport

log = logging.getLogger(__name__)


async def run_company_pipeline(
    company: Company,
    settings: Settings,
    cache: CacheManager,
    deduper,
    search_sem: asyncio.Semaphore,
    llm_sem: asyncio.Semaphore,
    mock_articles: list[Article] | None = None,
) -> CompanyReport:
    """Deterministic pipeline for one company; never raises (records failure)."""
    from nodes.analysis import AnalysisError, analyze_company, heuristic_analyze
    from nodes.crawl import extract_articles
    from nodes.search import search_company_news
    from utils.source_quality import rank_by_source_quality

    notes: list[str] = []
    providers: list[str] = []
    fallback_used = False

    try:
        if mock_articles is not None:
            articles = list(mock_articles)
            for a in articles:
                a.company, a.ticker = company.name, company.ticker
                a.evidence_excerpt = (a.content or "")[:500]
            providers = ["mock"]
            rank_by_source_quality(articles)  # deterministic, also useful in mock mode
        else:
            async with search_sem:
                outcome = await search_company_news(company, settings, cache)
            articles = outcome.articles
            providers = outcome.providers_used
            fallback_used = outcome.fallback_used
            if outcome.notes:
                notes.append(outcome.notes)

            if not articles:
                notes.append("No candidate articles found; skipping.")
                return CompanyReport(
                    company=company.name, ticker=company.ticker,
                    market_cap=company.market_cap, articles=[],
                    candidates_considered=0, providers_used=providers,
                    fallback_used=fallback_used, notes=" ".join(notes),
                )

            # Source-quality rank -> keep only the most promising for extraction.
            rank_by_source_quality(articles)
            pool = articles[: settings.max_articles_per_company + 5]
            articles = await extract_articles(pool, settings, cache)

        # Semantic dedup on extracted content, then trim to the analysis budget.
        articles = await deduper.deduplicate_async(articles)
        articles = articles[: settings.max_articles_per_company]

        if mock_articles is not None:
            ranked = heuristic_analyze(company, articles, top_n=settings.top_n_articles)
        else:
            async with llm_sem:
                try:
                    ranked = await analyze_company(company, articles, settings, cache)
                except AnalysisError as exc:
                    notes.append(str(exc))
                    log.error("[%s] %s — continuing with other companies", company.ticker, exc)
                    ranked = []

        return CompanyReport(
            company=company.name, ticker=company.ticker,
            market_cap=company.market_cap, articles=ranked,
            candidates_considered=len(articles), providers_used=providers,
            fallback_used=fallback_used, notes=" ".join(notes),
        )
    except Exception as exc:  # noqa: BLE001 — one company must never kill the run
        log.exception("[%s] unexpected pipeline failure", company.ticker)
        return CompanyReport(
            company=company.name, ticker=company.ticker,
            market_cap=company.market_cap, articles=[],
            candidates_considered=0, providers_used=providers,
            fallback_used=fallback_used,
            notes=f"Pipeline error: {exc}",
        )


async def execute_research_run(
    companies: list[Company],
    settings: Settings,
    cache: CacheManager,
    *,
    mock_articles: dict[str, list[Article]] | None = None,
    agentic: bool = False,
    on_company_complete: Callable[[CompanyReport], None] | None = None,
) -> list[CompanyReport]:
    """Run the pipeline for every company with bounded concurrency.

    Returns one :class:`CompanyReport` per company, in completion order.
    Per-company failures are recorded on the report, never raised. If the
    surrounding task is cancelled, ``CancelledError`` propagates (correct
    asyncio hygiene); reports finished before cancellation were already
    delivered via ``on_company_complete``, so the caller keeps the partials.
    """
    from nodes.deduplication import build_deduper

    deduper = build_deduper(settings)
    search_sem = asyncio.Semaphore(settings.max_search_concurrency)
    llm_sem = asyncio.Semaphore(settings.max_llm_concurrency)

    reports: list[CompanyReport] = []

    async def _wrapped(company: Company) -> CompanyReport:
        if agentic and mock_articles is None:
            from agent.agent import run_company_agent
            report = await run_company_agent(company, settings, cache)
        else:
            ma = mock_articles.get(company.ticker, []) if mock_articles is not None else None
            report = await run_company_pipeline(
                company, settings, cache, deduper, search_sem, llm_sem,
                mock_articles=ma,
            )
        reports.append(report)
        if on_company_complete is not None:
            on_company_complete(report)
        return report

    try:
        await asyncio.gather(*(_wrapped(c) for c in companies))
    except asyncio.CancelledError:
        log.warning("Research run cancelled; returning %d/%d partial reports",
                    len(reports), len(companies))
        raise
    return reports
