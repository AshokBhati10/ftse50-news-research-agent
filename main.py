#!/usr/bin/env python3
"""FTSE 50 Market News Research Agent — entry point.

Pipeline (default, cost-efficient):
    FTSE 100 -> top 50 by market cap -> per-company:
    SearXNG -> refined SearXNG -> You.com fallback -> + yfinance headlines ->
    URL dedup -> source-quality rank -> Crawl4AI extraction ->
    semantic dedup -> ONE batched Ollama analysis call -> top 3 ->
    JSON + Markdown reports.

Examples:
    uv run python main.py --mock                  # offline demo, no keys needed
    uv run python main.py --limit-companies 5     # cheap live test (needs keys)
    uv run python main.py --limit-companies 50   # full run
    uv run python main.py --evaluate              # run Pydantic Evals
    uv run python main.py --agentic --limit-companies 2   # agent drives tools
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Allow running as `python main.py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import Settings  # noqa: E402
from utils.cache import CacheManager  # noqa: E402
from utils.models import Article, Company, CompanyReport  # noqa: E402
from utils.source_quality import rank_by_source_quality  # noqa: E402

log = logging.getLogger("ftse50")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="FTSE 50 Market News Research Agent")
    p.add_argument("--limit-companies", type=int, default=50,
                   help="Max companies to research (default: 50). Use 5 for cheap tests.")
    p.add_argument("--companies", type=str, default="",
                   help="Comma-separated tickers to research instead of top-50, e.g. 'AZN.L,HSBA.L'.")
    p.add_argument("--mock", action="store_true",
                   help="Offline mock mode: bundled data + heuristic analysis, no network/keys.")
    p.add_argument("--evaluate", action="store_true",
                   help="Run the Pydantic Evals suite and exit.")
    p.add_argument("--agentic", action="store_true",
                   help="Let the Pydantic AI agent drive per-company research via tools.")
    p.add_argument("--no-cache", action="store_true", help="Disable all caching.")
    p.add_argument("--output-dir", type=str, default="",
                   help="Override output directory for reports.")
    p.add_argument("--log-level", type=str, default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Never let chatty libraries drown the run log (or leak URLs with keys).
    for noisy in ("httpx", "httpcore", "yfinance", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Mock mode data
# ---------------------------------------------------------------------------

def load_mock_companies(settings: Settings) -> tuple[list[Company], dict[str, list[Article]]]:
    payload = json.loads(Path(settings.mock_data_path).read_text(encoding="utf-8"))
    companies = [Company(**c) for c in payload["companies"]]
    articles: dict[str, list[Article]] = {}
    for ticker, items in payload["articles"].items():
        arts = []
        for it in items:
            arts.append(Article(
                company="", ticker=ticker,
                title=it["title"], url=it["url"],
                source=it.get("source", ""),
                published_at=it.get("published_at"),
                snippet=it.get("snippet", ""),
                content=it.get("content"),
                search_provider="mock",
                extraction_status="mock",
            ))
        articles[ticker] = arts
    return companies, articles


# ---------------------------------------------------------------------------
# Per-company pipeline
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def amain(args: argparse.Namespace) -> int:
    from nodes.deduplication import build_deduper
    from nodes.ingestion import build_ftse50
    from nodes.output import build_full_report, write_reports

    settings = Settings()
    if args.output_dir:
        settings.output_dir = Path(args.output_dir)

    cache = CacheManager(settings.cache_dir, enabled=not args.no_cache)
    use_mock = args.mock

    if use_mock:
        companies, mock_map = load_mock_companies(settings)
        log.info("Mock mode: %d companies from %s", len(companies), settings.mock_data_path)
    else:
        if args.agentic:
            settings.require_live_llm()
        companies = await build_ftse50(settings, cache)
        mock_map = {}
        if args.companies:
            wanted = {t.strip().upper() for t in args.companies.split(",") if t.strip()}
            # Allow short EPICs too (AZN -> AZN.L).
            wanted |= {f"{t}.L" for t in wanted if "." not in t}
            companies = [c for c in companies if c.ticker.upper() in wanted]
            if not companies:
                log.error("None of the requested tickers are in the FTSE 50 list: %s", wanted)
                return 2
        companies = companies[: args.limit_companies]

    if not companies:
        log.error("No companies to research.")
        return 2

    log.info("Researching %d companies …", len(companies))
    deduper = build_deduper(settings)
    search_sem = asyncio.Semaphore(settings.max_search_concurrency)
    llm_sem = asyncio.Semaphore(settings.max_llm_concurrency)

    use_rich = False
    progress = None
    task_id = None
    try:
        from rich.progress import Progress
        progress = Progress()
        progress.start()
        task_id = progress.add_task("Researching companies", total=len(companies))
        use_rich = True
    except Exception:
        pass

    async def _wrapped(company: Company) -> CompanyReport:
        try:
            if args.agentic and not use_mock:
                from agent.agent import run_company_agent
                return await run_company_agent(company, settings, cache)
            return await run_company_pipeline(
                company, settings, cache, deduper, search_sem, llm_sem,
                mock_articles=mock_map.get(company.ticker, []) if use_mock else None,
            )
        finally:
            if use_rich:
                progress.update(task_id, advance=1)

    reports = await asyncio.gather(*(_wrapped(c) for c in companies))
    if use_rich:
        progress.stop()

    full = build_full_report(list(reports), settings)
    json_path, md_path = write_reports(full, settings.output_dir)

    _print_summary(full, json_path, md_path)
    failed = sum(1 for r in reports if not r.articles)
    log.info("Done: %d/%d companies produced top-%d lists (%d with no articles).",
             len(reports) - failed, len(reports), settings.top_n_articles, failed)
    return 0


def _print_summary(full, json_path: Path, md_path: Path) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
    except Exception:
        print(f"Wrote {json_path} and {md_path}")
        return
    console = Console()
    table = Table(title="FTSE 50 News Digest — run summary")
    table.add_column("Ticker")
    table.add_column("Candidates", justify="right")
    table.add_column("Top headline")
    table.add_column("Impact")
    for c in full.companies:
        top = c.articles[0] if c.articles else None
        table.add_row(
            c.ticker,
            str(c.candidates_considered),
            (top.title[:70] if top else (c.notes[:70] or "—")),
            (top.impact.value if top else "—"),
        )
    console.print(table)
    console.print(f"[green]JSON:[/green] {json_path}")
    console.print(f"[green]Markdown:[/green] {md_path}")


def main() -> int:
    args = parse_args()
    setup_logging(args.log_level)
    if args.evaluate:
        # Synchronous: pydantic-evals manages its own event loop.
        from evaluation import run_evaluations
        return run_evaluations(Settings())
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        log.warning("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
