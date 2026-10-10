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
    p.add_argument("--query", type=str, default="",
                   help="Query-driven research: arbitrary topic/question instead of "
                        "FTSE companies, e.g. --query 'Latest developments in "
                        "semiconductor stocks'.")
    p.add_argument("--max-results", type=int, default=20,
                   help="Max articles to analyse for --query (1-50, default: 20).")
    p.add_argument("--time-range", type=str, default="month",
                   choices=["day", "week", "month", "year"],
                   help="Recency bias for --query search (default: month).")
    p.add_argument("--fresh", action="store_true",
                   help="With --query: bypass cached search results.")
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
# Per-company pipeline (shared with the API — see nodes/runner.py)
# ---------------------------------------------------------------------------

from nodes.runner import execute_research_run, run_company_pipeline  # noqa: F401


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Query-driven research (CLI)
# ---------------------------------------------------------------------------

async def _run_query(args: argparse.Namespace, settings: Settings,
                     cache: CacheManager) -> int:
    """Execute a query-driven research run from the CLI."""
    from datetime import datetime, timezone
    from nodes.output import write_query_reports
    from nodes.runner import QUERY_STAGES, execute_query_research

    query = args.query.strip()
    log.info("Researching query: %r (max_results=%d, time_range=%s, fresh=%s)",
             query, args.max_results, args.time_range, args.fresh)

    def _on_stage(stage: str, info: dict) -> None:
        log.info("[query:%s] %s", stage, {k: v for k, v in info.items()
                                          if k in ("collected", "extracted",
                                                   "candidates", "findings",
                                                   "duplicates_removed")})

    run_id = f"cli_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    report = await execute_query_research(
        query, settings, cache, run_id=run_id,
        max_results=args.max_results, time_range=args.time_range,
        fresh=args.fresh, on_stage=_on_stage,
    )
    json_path, md_path = write_query_reports(report, settings.output_dir)
    log.info("Done: %d findings for %r.", len(report.findings), query)
    log.info("Wrote %s and %s", json_path, md_path)
    for w in report.warnings:
        log.warning("warning: %s", w)
    return 0


async def amain(args: argparse.Namespace) -> int:
    from nodes.deduplication import build_deduper
    from nodes.ingestion import build_ftse50
    from nodes.output import build_full_report, write_reports

    settings = Settings()
    if args.output_dir:
        settings.output_dir = Path(args.output_dir)

    cache = CacheManager(settings.cache_dir, enabled=not args.no_cache)
    use_mock = args.mock

    # Query-driven research takes precedence over the company workflow.
    if args.query != "" and not args.query.strip():
        log.error("--query must not be blank.")
        return 2
    if args.query.strip():
        if args.agentic:
            log.error("--agentic is only supported for FTSE company research, not --query.")
            return 2
        if not 1 <= args.max_results <= 50:
            log.error("--max-results must be between 1 and 50.")
            return 2
        settings.require_live_llm()
        return await _run_query(args, settings, cache)

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

    def _on_complete(report: CompanyReport) -> None:
        if use_rich:
            progress.update(task_id, advance=1)

    reports = await execute_research_run(
        companies,
        settings,
        cache,
        mock_articles=mock_map if use_mock else None,
        agentic=args.agentic,
        on_company_complete=_on_complete,
    )
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
