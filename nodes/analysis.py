"""Node 4 — Financial-materiality analysis.

The LLM does *reasoning* work only: judging which stories are financially /
materially important. Everything deterministic (filtering, dedup, ranking,
selection) stays in plain Python.

Cost discipline:
- One batched Pydantic AI call per company (never one call per article).
- Candidates are date-filtered, URL-deduplicated, semantically deduplicated
  and source-ranked *before* this stage.
- Concise prompt, short excerpts, structured output (no free-form JSON parse).
- Analysis results are cached (keyed by ticker + article set + model).

Failure policy: bounded exponential backoff (configurable retries); if the
LLM is still failing, the company is recorded as failed and the run continues
with the remaining companies.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from config.settings import Settings
from utils.cache import CacheManager
from utils.models import (
    AnalysisResult,
    Article,
    Company,
    ImpactLevel,
    RankedArticle,
    Sentiment,
)
from utils.rate_limiter import retry_async

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a senior equity research analyst covering UK large-cap stocks.
You rank news articles by their potential to move the company's share price —
financial MATERIALITY, not emotional tone and not clickbait appeal.

High-impact examples: major earnings surprise, M&A / takeover, profit warning,
material guidance change, major regulatory decision or fine, CEO departure,
major litigation outcome, drug/product approval, large contract win/loss,
dividend cut or suspension, significant geopolitical or commodity exposure.

Routine items (minor insider trades, generic market commentary, republished
press releases with no new facts, lifestyle pieces) are Low impact unless a
clear financial consequence is stated.

For EACH article, judge:
- impact: High / Medium / Low  (financial materiality)
- sentiment: Positive / Negative / Neutral  (for the company's outlook)
- reason: one concise sentence justifying the impact judgement
- confidence: 0.0-1.0

Be discriminating: most daily news is Low/Medium. Reserve High for genuinely
market-moving stories.
"""

_ARTICLE_BLOCK = """\
[{index}] {title}
    Source: {source} | Date: {date}
    {excerpt}
"""


def build_analysis_prompt(company: Company, articles: list[Article]) -> str:
    blocks = []
    for i, a in enumerate(articles):
        date = a.published_at.strftime("%Y-%m-%d") if a.published_at else "unknown date"
        blocks.append(_ARTICLE_BLOCK.format(
            index=i,
            title=a.title,
            source=a.source or "unknown",
            date=date,
            excerpt=a.text_for_analysis(600).replace("\n", " "),
        ))
    return (
        f"Company: {company.name} ({company.ticker})\n"
        f"Score each of the {len(articles)} articles below. "
        "Return a score entry for every article index.\n\n"
        + "\n".join(blocks)
    )


def build_analysis_agent(settings: Settings):
    """Pydantic AI agent backed by the Ollama OpenAI-compatible endpoint."""
    from pydantic_ai import Agent
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    provider = OpenAIProvider(
        base_url=settings.ollama_base_url.rstrip("/"),
        api_key=settings.ollama_api_key,
    )
    model = OpenAIChatModel(settings.ollama_model, provider=provider)
    return Agent(model, output_type=AnalysisResult, system_prompt=SYSTEM_PROMPT)


class AnalysisError(RuntimeError):
    pass


_IMPACT_RANK = {ImpactLevel.HIGH: 0, ImpactLevel.MEDIUM: 1, ImpactLevel.LOW: 2}


def _analysis_cache_key(company: Company, articles: list[Article], model: str) -> str:
    fingerprint = "|".join(sorted(a.url for a in articles))
    digest = hashlib.sha256(fingerprint.encode()).hexdigest()[:24]
    return f"{company.ticker}:{model}:{digest}"


def select_top_n(
    company: Company,
    articles: list[Article],
    result: AnalysisResult,
    top_n: int,
) -> list[RankedArticle]:
    """Deterministically pick the top-N articles from LLM scores."""
    by_index = {s.index: s for s in result.scores}
    scored = [
        (by_index[i], articles[i])
        for i in range(len(articles))
        if i in by_index
    ]
    if not scored:
        raise AnalysisError("LLM returned no scores matching submitted articles")

    def _key(pair) -> tuple:
        score, article = pair
        return (
            _IMPACT_RANK[score.impact],
            -score.confidence,
            -article.source_quality,
        )

    scored.sort(key=_key)
    ranked: list[RankedArticle] = []
    for score, article in scored[:top_n]:
        ranked.append(RankedArticle(
            title=article.title,
            url=article.url,
            source=article.source,
            published_at=article.published_at,
            impact=score.impact,
            sentiment=score.sentiment,
            reason=score.reason,
            confidence=score.confidence,
            source_quality=article.source_quality,
            evidence_excerpt=article.evidence_excerpt or article.text_for_analysis(500),
        ))
    return ranked


async def analyze_company(
    company: Company,
    articles: list[Article],
    settings: Settings,
    cache: CacheManager | None = None,
    agent=None,
) -> list[RankedArticle]:
    """One batched LLM analysis call per company. Raises AnalysisError on failure."""
    if not articles:
        return []
    settings.require_live_llm()

    cache_key = _analysis_cache_key(company, articles, settings.ollama_model)
    if cache:
        hit = cache.analysis.get("analysis", cache_key)
        if hit:
            log.info("[%s] using cached LLM analysis", company.ticker)
            return [RankedArticle(**r) for r in hit]

    agent = agent or build_analysis_agent(settings)
    prompt = build_analysis_prompt(company, articles)

    async def _call() -> AnalysisResult:
        run = await agent.run(prompt)
        return run.output

    try:
        result = await retry_async(
            _call,
            max_retries=settings.llm_max_retries,
            base_delay_s=2.0,
            retry_on=(Exception,),
            description=f"Ollama analysis for {company.ticker}",
        )
    except Exception as exc:
        raise AnalysisError(f"LLM analysis failed for {company.ticker}: {exc}") from exc

    ranked = select_top_n(company, articles, result, settings.top_n_articles)
    if cache:
        cache.analysis.set("analysis", cache_key, [r.model_dump(mode="json") for r in ranked])
    log.info("[%s] analysis complete: %d candidates -> %d ranked",
             company.ticker, len(articles), len(ranked))
    return ranked


# ---------------------------------------------------------------------------
# Deterministic heuristic analyzer — mock/offline mode only.
# ---------------------------------------------------------------------------

_HIGH_IMPACT_KEYWORDS = (
    "merger", "acquisition", "acquire", "takeover", "buyout", "merges",
    "earnings beat", "beats estimates", "profit warning", "guidance raised",
    "guidance cut", "cuts guidance", "raises guidance",
    "fda approval", "approved", "ceo resign", "ceo departure", "chief executive resign",
    "dividend cut", "dividend suspend", "suspends dividend",
    "regulatory approval", "antitrust", "fine", "lawsuit", "litigation",
    "contract win", "contract worth", "buyback",
)

_MEDIUM_IMPACT_KEYWORDS = (
    "earnings", "results", "quarterly", "dividend", "guidance",
    "forecast", "outlook", "upgrade", "downgrade", "price target",
    "stake", "investment", "expansion", "partnership",
)

_POSITIVE_WORDS = (
    "beat", "beats", "record", "growth", "grows", "raise", "raised", "raises",
    "approval", "approved", "upgrade", "upgraded", "profit", "surge", "surged",
    "win", "wins", "strong", "exceed",
)

_NEGATIVE_WORDS = (
    "miss", "misses", "cut", "cuts", "warning", "warns", "loss", "losses",
    "fall", "falls", "drop", "drops", "decline", "probe", "fine", "fined",
    "resign", "resigns", "downgrade", "downgraded", "lawsuit", "weak",
)


def heuristic_analyze(company: Company, articles: list[Article], top_n: int = 3) -> list[RankedArticle]:
    """Rule-based impact/sentiment scoring. Deterministic; for --mock only."""
    def _score(a: Article) -> tuple[ImpactLevel, Sentiment, float]:
        text = f"{a.title} {a.snippet}".lower()
        if any(k in text for k in _HIGH_IMPACT_KEYWORDS):
            impact = ImpactLevel.HIGH
        elif any(k in text for k in _MEDIUM_IMPACT_KEYWORDS):
            impact = ImpactLevel.MEDIUM
        else:
            impact = ImpactLevel.LOW
        pos = sum(1 for w in _POSITIVE_WORDS if w in text)
        neg = sum(1 for w in _NEGATIVE_WORDS if w in text)
        if pos > neg:
            sentiment = Sentiment.POSITIVE
        elif neg > pos:
            sentiment = Sentiment.NEGATIVE
        else:
            sentiment = Sentiment.NEUTRAL
        confidence = 0.6 if impact != ImpactLevel.LOW else 0.45
        return impact, sentiment, confidence

    ranked: list[RankedArticle] = []
    for a in articles:
        impact, sentiment, confidence = _score(a)
        ranked.append(RankedArticle(
            title=a.title, url=a.url, source=a.source,
            published_at=a.published_at, impact=impact, sentiment=sentiment,
            reason=f"Heuristic mock scoring ({impact.value} impact, {sentiment.value}).",
            confidence=confidence, source_quality=a.source_quality,
            evidence_excerpt=a.evidence_excerpt or a.text_for_analysis(500),
        ))
    ranked.sort(key=lambda r: (_IMPACT_RANK[r.impact], -r.confidence, -r.source_quality))
    return ranked[:top_n]
