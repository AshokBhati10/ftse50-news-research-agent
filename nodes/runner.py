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
from utils.models import Article, Company, CompanyReport, QueryReport

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


# ---------------------------------------------------------------------------
# Query-driven research orchestration
# ---------------------------------------------------------------------------

QUERY_STAGES = ("search", "extract", "deduplicate", "analyze", "report")


async def execute_query_research(
    query: str,
    settings: Settings,
    cache: CacheManager,
    *,
    run_id: str = "",
    max_results: int = 20,
    time_range: str = "month",
    fresh: bool = False,
    agentic: bool = True,
    on_stage: Callable[[str, dict], None] | None = None,
) -> QueryReport:
    """Run the query-driven pipeline.

    ``agentic=True`` (default) uses the ResearchAgent: task-specific planning,
    a bounded tool-calling loop, and an answer-first report. ``agentic=False``
    keeps the original deterministic pipeline (search -> extract -> dedup ->
    one batched analysis -> ranked findings).
    """
    if agentic:
        return await _execute_agentic_query(
            query, settings, cache, run_id=run_id, max_results=max_results,
            time_range=time_range, fresh=fresh, on_stage=on_stage,
        )
    return await _execute_simple_query(
        query, settings, cache, run_id=run_id, max_results=max_results,
        time_range=time_range, fresh=fresh, on_stage=on_stage,
    )


async def _execute_agentic_query(
    query: str,
    settings: Settings,
    cache: CacheManager,
    *,
    run_id: str,
    max_results: int,
    time_range: str,
    fresh: bool,
    on_stage: Callable[[str, dict], None] | None,
) -> QueryReport:
    """Agentic query research via ResearchAgent (plan -> tools -> answer)."""
    from datetime import datetime, timezone
    from agent.research_agent import ResearchAgent
    from nodes.output import build_answer_report

    started_at = datetime.now(timezone.utc)
    agent = ResearchAgent(settings, cache, run_id=run_id or "-")
    result = await agent.research(
        query, max_results=max_results, time_range=time_range,
        fresh=fresh, on_stage=on_stage,
    )
    # Detailed mode: also produce ranked findings from the gathered evidence.
    if result.plan.detailed and result.evidence:
        result.findings = await _findings_from_evidence(
            query, result.evidence, settings, cache, max_results)
    return build_answer_report(
        query=query, run_id=run_id, started_at=started_at,
        result=result, settings=settings,
    )


async def _findings_from_evidence(query: str, evidence: list[dict],
                                  settings: Settings, cache: CacheManager,
                                  max_results: int) -> list:
    """Ranked findings for detailed mode, from agent-gathered evidence."""
    from dateutil import parser as date_parser
    from nodes.analysis import analyze_query
    from utils.models import Article

    articles: list[Article] = []
    seen: set[str] = set()
    for e in evidence:
        url = e.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        published = None
        if e.get("published_at"):
            try:
                published = date_parser.parse(str(e["published_at"]))
            except (ValueError, OverflowError):
                published = None
        text = e.get("text", "")
        articles.append(Article(
            company="", ticker="", title=e.get("title", "") or url,
            url=url, source=e.get("source", ""),
            published_at=published, snippet=text[:500],
            content=text if e.get("kind") == "article" else None,
            search_provider="agent",
            extraction_status="ok" if text else "snippet_only",
            source_quality=float(e.get("source_quality", 0.0) or 0.0),
        ))
    if not articles:
        return []
    try:
        return await analyze_query(query, articles[:max_results], settings, cache)
    except Exception as exc:  # noqa: BLE001 — findings are optional
        log.warning("Detailed findings failed: %s", exc)
        return []


async def _execute_simple_query(
    query: str,
    settings: Settings,
    cache: CacheManager,
    *,
    run_id: str,
    max_results: int,
    time_range: str,
    fresh: bool,
    on_stage: Callable[[str, dict], None] | None,
) -> QueryReport:
    """Original deterministic query pipeline (no agent loop)."""
    from datetime import datetime, timezone
    from nodes.analysis import AnalysisError, analyze_query
    from nodes.crawl import extract_articles
    from nodes.deduplication import build_deduper
    from nodes.output import build_query_report
    from nodes.search import search_topic
    from utils.source_quality import rank_by_source_quality

    query = query.strip()
    if not query:
        raise ValueError("Query must not be empty")
    started_at = datetime.now(timezone.utc)
    warnings: list[str] = []

    def _emit(stage: str, info: dict) -> None:
        if on_stage is not None:
            on_stage(stage, info)

    # --- search -----------------------------------------------------------
    outcome = await search_topic(
        query, settings, cache,
        max_results=max_results, time_range=time_range, fresh=fresh,
    )
    if outcome.notes:
        warnings.extend(n for n in outcome.notes.split("; ") if n)
    if outcome.from_cache:
        warnings.append("Search results were served from cache (fresh search off).")
    _emit("search", {
        "queries_used": outcome.queries_used,
        "collected": len(outcome.articles),
        "providers": outcome.providers_used,
        "from_cache": outcome.from_cache,
    })
    collected = len(outcome.articles)

    # --- source-quality rank + extraction ----------------------------------
    rank_by_source_quality(outcome.articles)
    pool = outcome.articles[: max_results + 5]
    extracted = await extract_articles(pool, settings, cache)
    _emit("extract", {"extracted": len(extracted), "attempted": len(pool)})
    extraction_failures = sum(1 for a in extracted if a.extraction_status == "failed")
    if extraction_failures:
        warnings.append(f"{extraction_failures} article(s) failed extraction "
                        "and were excluded.")

    # --- semantic dedup ----------------------------------------------------
    deduper = build_deduper(settings)
    deduped = await deduper.deduplicate_async(extracted)
    duplicates_removed = len(extracted) - len(deduped)
    # Failed extractions carry no usable text; exclude from analysis but count.
    analyzable = [a for a in deduped if a.extraction_status != "failed"]
    excluded_failed = len(deduped) - len(analyzable)
    if excluded_failed:
        warnings.append(f"{excluded_failed} article(s) had no extractable text "
                        "and were excluded from analysis.")
    candidates = analyzable[:max_results]
    _emit("deduplicate", {
        "candidates": len(candidates),
        "duplicates_removed": duplicates_removed,
    })

    # --- LLM analysis -------------------------------------------------------
    findings: list = []
    if candidates:
        try:
            findings = await analyze_query(query, candidates, settings, cache)
        except AnalysisError as exc:
            warnings.append(str(exc))
            log.error("[query] %s — returning partial report", exc)
    else:
        warnings.append("No extractable articles found for this query.")
    _emit("analyze", {"findings": len(findings)})

    counts = {
        "max_results": max_results,
        "collected": collected,
        "extracted": len(extracted),
        "duplicates_removed": duplicates_removed,
        "analyzed": len(candidates),
    }
    report = build_query_report(
        query=query, run_id=run_id, started_at=started_at,
        outcome=outcome, findings=findings, counts=counts,
        warnings=warnings, settings=settings,
    )
    _emit("report", {"findings": len(findings)})
    return report
