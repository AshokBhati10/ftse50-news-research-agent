"""Pydantic AI agent layer.

Two usage modes:

1. **Pipeline mode (default, cost-efficient):** ``main.py`` runs the
   deterministic pipeline (search -> dedup -> source-rank -> crawl ->
   semantic dedup) in plain Python with bounded concurrency, then makes
   *one* batched Pydantic AI analysis call per company
   (:func:`nodes.analysis.analyze_company`).

2. **Agentic mode (``--agentic``):** a single Pydantic AI agent drives the
   whole per-company research loop itself — choosing when to search, refine,
   fall back, fetch articles and finally emitting a validated
   :class:`CompanyReport`. Useful for interactive/ad-hoc research; more LLM
   tokens per company, so it is opt-in.

Tools are only defined where reasoning/tool selection adds value; all
deterministic work lives in ``nodes/`` and ``utils/``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from pydantic_ai import Agent, RunContext

from config.settings import Settings
from nodes import ingestion, search as search_node
from nodes.analysis import SYSTEM_PROMPT as ANALYST_SYSTEM_PROMPT
from nodes.crawl import extract_article
from utils.cache import CacheManager
from utils.models import Article, Company, CompanyReport, RankedArticle

log = logging.getLogger(__name__)

RESEARCHER_SYSTEM_PROMPT = """\
You are an autonomous financial news researcher covering UK large-cap stocks.

You have tools to search for news (SearXNG primary, You.com fallback),
fetch supplementary headlines, and extract full article text.

Process for the assigned company:
1. Call search_news with a strong query. Aim for ~10 candidate articles.
2. If results are thin (< 4 useful), refine the query and search again.
3. Only if SearXNG is still insufficient, call fallback_search (You.com).
4. Call get_supplementary_news for extra headlines.
5. Call fetch_article for the most promising URLs (prefer reputable sources).
6. Finally, analyse the collected articles YOURSELF as a senior equity analyst:
   rank by potential to move the share price (financial materiality, not hype),
   classify impact (High/Medium/Low) and sentiment (Positive/Negative/Neutral),
   give a one-sentence reason and confidence for each, and return the top 3
   as the structured CompanyReport output.

High-impact examples: earnings surprise, M&A, profit warning, guidance change,
regulatory decisions, CEO departure, major litigation, drug approval, dividend cut.
Most daily news is Low/Medium — reserve High for genuinely market-moving stories.

Every finding must keep its evidence: title, URL, source, publication date and
a short supporting excerpt.
"""


@dataclass
class ResearchDeps:
    settings: Settings
    cache: CacheManager
    company: Company
    articles: list[Article] = field(default_factory=list)
    providers_used: list[str] = field(default_factory=list)
    fallback_used: bool = False


def build_research_agent(settings: Settings) -> Agent[ResearchDeps, CompanyReport]:
    """Build the agentic researcher (Ollama-backed, structured output)."""
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    settings.require_live_llm()
    provider = OpenAIProvider(
        base_url=settings.ollama_base_url.rstrip("/"),
        api_key=settings.ollama_api_key,
    )
    model = OpenAIChatModel(settings.ollama_model, provider=provider)
    agent = Agent(
        model,
        deps_type=ResearchDeps,
        output_type=CompanyReport,
        system_prompt=RESEARCHER_SYSTEM_PROMPT + "\n\n" + ANALYST_SYSTEM_PROMPT,
        retries=2,
    )

    # ------------------------------------------------------------------ tools
    @agent.tool
    async def get_ftse_companies(ctx: RunContext[ResearchDeps], limit: int = 50) -> str:
        """List the top FTSE companies by market capitalisation."""
        companies = await ingestion.build_ftse50(ctx.deps.settings, ctx.deps.cache, limit=limit)
        lines = [f"{c.ticker}: {c.name}" for c in companies]
        return "\n".join(lines)

    @agent.tool
    async def search_news(ctx: RunContext[ResearchDeps], query: str) -> str:
        """Search SearXNG (primary) for company news. Returns a summary."""
        articles = await search_node.searxng_search(
            query, ctx.deps.settings, max_results=ctx.deps.settings.max_articles_per_company
        )
        company = ctx.deps.company
        for a in articles:
            a.company, a.ticker = company.name, company.ticker
        before = len(ctx.deps.articles)
        ctx.deps.articles = search_node.dedup_by_url(ctx.deps.articles + articles)
        if "searxng" not in ctx.deps.providers_used:
            ctx.deps.providers_used.append("searxng")
        added = len(ctx.deps.articles) - before
        return (f"SearXNG returned {len(articles)} results, {added} new stored "
                f"({len(ctx.deps.articles)} total). Titles: "
                + "; ".join(a.title[:80] for a in articles[:5]))

    @agent.tool
    async def fallback_search(ctx: RunContext[ResearchDeps], query: str) -> str:
        """You.com fallback search — use ONLY when SearXNG results are insufficient."""
        articles = await search_node.youcom_search(
            query, ctx.deps.settings, max_results=ctx.deps.settings.max_articles_per_company
        )
        company = ctx.deps.company
        for a in articles:
            a.company, a.ticker = company.name, company.ticker
        before = len(ctx.deps.articles)
        ctx.deps.articles = search_node.dedup_by_url(ctx.deps.articles + articles)
        if "you.com" not in ctx.deps.providers_used:
            ctx.deps.providers_used.append("you.com")
        ctx.deps.fallback_used = True
        added = len(ctx.deps.articles) - before
        return f"You.com returned {len(articles)} results, {added} new stored."

    @agent.tool
    async def get_supplementary_news(ctx: RunContext[ResearchDeps]) -> str:
        """Fetch supplementary headlines from yfinance for the company."""
        articles = await search_node.yfinance_news(ctx.deps.company)
        before = len(ctx.deps.articles)
        ctx.deps.articles = search_node.dedup_by_url(ctx.deps.articles + articles)
        if "yfinance" not in ctx.deps.providers_used:
            ctx.deps.providers_used.append("yfinance")
        added = len(ctx.deps.articles) - before
        return f"yfinance returned {len(articles)} headlines, {added} new stored."

    @agent.tool
    async def fetch_article(ctx: RunContext[ResearchDeps], url: str) -> str:
        """Extract the full text of one collected article URL. Returns an excerpt."""
        match = next((a for a in ctx.deps.articles if a.url == url), None)
        if match is None:
            return f"URL not in collected articles: {url}"
        await extract_article(match, ctx.deps.settings, ctx.deps.cache)
        excerpt = (match.content or match.snippet or "")[:800]
        return f"[{match.extraction_status}] {match.title}\n{excerpt}"

    return agent


async def run_company_agent(
    company: Company,
    settings: Settings,
    cache: CacheManager,
) -> CompanyReport:
    """Fully agentic per-company research: the agent drives tools, then reports."""
    agent = build_research_agent(settings)
    deps = ResearchDeps(settings=settings, cache=cache, company=company)
    log.info("[%s] starting agentic research", company.ticker)
    run = await agent.run(
        f"Research the latest market-moving news for {company.name} ({company.ticker}).",
        deps=deps,
    )
    report: CompanyReport = run.output
    # Fill bookkeeping the agent cannot know precisely.
    report.company = company.name
    report.ticker = company.ticker
    report.market_cap = company.market_cap
    report.candidates_considered = len(deps.articles)
    report.providers_used = deps.providers_used
    report.fallback_used = deps.fallback_used
    return report


# Convenience re-export for pipeline code that wants the tool functions' logic.
async def analyze_with_dedicated_agent(
    company: Company,
    articles: list[Article],
    settings: Settings,
    cache: CacheManager | None = None,
) -> list[RankedArticle]:
    from nodes.analysis import analyze_company

    return await analyze_company(company, articles, settings, cache)


__all__ = [
    "ResearchDeps",
    "build_research_agent",
    "run_company_agent",
    "analyze_with_dedicated_agent",
]
