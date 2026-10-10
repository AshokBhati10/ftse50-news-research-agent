"""Node 2 — News retrieval.

Search cascade per company (cost-aware — never call both providers per company):

    SearXNG (primary query)
      -> insufficient? refined SearXNG query
        -> still insufficient AND You.com key configured? You.com fallback
    + yfinance ``Ticker.news`` as supplementary headlines (always attempted)

Results are merged into one normalized :class:`Article` list with URL-level
deduplication. Recent-news bias is applied deterministically (SearXNG
``time_range`` + a hard publication-date cutoff) *before* anything expensive.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
import yfinance as yf
from dateutil import parser as date_parser
from pydantic import BaseModel, Field

from config.settings import Settings
from utils import query_builder
from utils.cache import CacheManager
from utils.models import Article, Company
from utils.rate_limiter import AsyncRateLimiter, retry_async

log = logging.getLogger(__name__)

YOUCOM_SEARCH_URL = "https://api.ydc-index.io/search"

_searxng_limiter = AsyncRateLimiter(min_interval_s=1.0)
_youcom_limiter = AsyncRateLimiter(min_interval_s=1.0)


class SearchError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(value: object) -> datetime | None:
    if not value:
        return None
    try:
        dt = date_parser.parse(str(value))
    except (ValueError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _recent_enough(published: datetime | None, lookback_days: int) -> bool:
    if published is None:
        return True  # keep undated items; ranking prefers dated ones
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    return published >= cutoff


def _source_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def normalize_url(url: str) -> str:
    """Canonical form for URL-level deduplication."""
    try:
        parts = urlparse(url.strip())
    except Exception:
        return url.strip().lower()
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    # Drop tracking parameters; keep the rest so distinct articles stay distinct.
    drop = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
            "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "output"}
    query = "&".join(
        p for p in (parts.query or "").split("&")
        if p and p.split("=")[0].lower() not in drop
    )
    normalized = f"{parts.scheme.lower()}://{host}{path}"
    return f"{normalized}?{query}" if query else normalized


def dedup_by_url(articles: list[Article]) -> list[Article]:
    seen: set[str] = set()
    unique: list[Article] = []
    for a in articles:
        key = normalize_url(a.url)
        if key in seen:
            continue
        seen.add(key)
        unique.append(a)
    return unique


# ---------------------------------------------------------------------------
# SearXNG (primary)
# ---------------------------------------------------------------------------

async def searxng_search(
    query: str,
    settings: Settings,
    max_results: int = 10,
    time_range: str = "month",
) -> list[Article]:
    """Query SearXNG's JSON API. Raises SearchError on transport/API failure."""
    params = {
        "q": query,
        "format": "json",
        "language": "en",
        "time_range": time_range,  # bias toward recent news
        "safesearch": "0",
    }

    async def _call() -> list[Article]:
        async with _searxng_limiter:
            async with httpx.AsyncClient(timeout=settings.searxng_timeout_s) as client:
                resp = await client.get(settings.searxng_search_url, params=params)
        resp.raise_for_status()
        payload = resp.json()
        results = payload.get("results") or []
        articles: list[Article] = []
        for r in results[:max_results]:
            url = (r.get("url") or "").strip()
            title = (r.get("title") or "").strip()
            if not url or not title:
                continue
            articles.append(Article(
                company="", ticker="",
                title=title,
                url=url,
                source=_source_from_url(url),
                published_at=_parse_date(r.get("publishedDate")),
                snippet=(r.get("content") or "").strip(),
                search_provider="searxng",
            ))
        return articles

    try:
        return await retry_async(
            _call, max_retries=1, base_delay_s=1.0,
            retry_on=(httpx.HTTPError, ValueError),
            description=f"SearXNG search {query!r}",
        )
    except Exception as exc:
        raise SearchError(f"SearXNG failed for {query!r}: {exc}") from exc


# ---------------------------------------------------------------------------
# You.com (fallback only)
# ---------------------------------------------------------------------------

async def youcom_search(
    query: str,
    settings: Settings,
    max_results: int = 10,
) -> list[Article]:
    """You.com web search — fallback path only. Raises SearchError on failure."""
    if not settings.you_api_key:
        raise SearchError("You.com fallback requested but YOU_API_KEY is not configured")

    async def _call() -> list[Article]:
        async with _youcom_limiter:
            async with httpx.AsyncClient(timeout=settings.you_timeout_s) as client:
                resp = await client.get(
                    YOUCOM_SEARCH_URL,
                    params={"query": query, "num_web_results": max_results},
                    headers={"X-API-Key": settings.you_api_key},
                )
        resp.raise_for_status()
        payload = resp.json()
        hits = (payload.get("results") or {}).get("results") or payload.get("results") or []
        articles: list[Article] = []
        for h in hits[:max_results]:
            url = (h.get("url") or "").strip()
            title = (h.get("title") or "").strip()
            if not url or not title:
                continue
            snippets = h.get("snippets") or []
            articles.append(Article(
                company="", ticker="",
                title=title,
                url=url,
                source=_source_from_url(url),
                published_at=_parse_date(h.get("page_age") or h.get("published_date")),
                snippet=" ".join(s.strip() for s in snippets if isinstance(s, str)).strip(),
                search_provider="you.com",
            ))
        return articles

    try:
        return await retry_async(
            _call, max_retries=1, base_delay_s=2.0,
            retry_on=(httpx.HTTPError, ValueError),
            description=f"You.com search {query!r}",
        )
    except Exception as exc:
        raise SearchError(f"You.com failed for {query!r}: {exc}") from exc


# ---------------------------------------------------------------------------
# yfinance supplementary news
# ---------------------------------------------------------------------------

async def yfinance_news(company: Company, max_results: int = 10) -> list[Article]:
    """Supplementary headlines from yfinance — never raises."""

    def _blocking() -> list[Article]:
        try:
            items = yf.Ticker(company.ticker).news or []
        except Exception as exc:  # noqa: BLE001
            log.warning("yfinance news failed for %s: %s", company.ticker, exc)
            return []
        articles: list[Article] = []
        for item in items[:max_results]:
            url = str(item.get("link") or "").strip()
            title = str(item.get("title") or "").strip()
            if not url or not title:
                continue
            ts = item.get("providerPublishTime")
            published = None
            if ts:
                try:
                    published = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                except (ValueError, OSError):
                    published = None
            articles.append(Article(
                company=company.name, ticker=company.ticker,
                title=title, url=url,
                source=str(item.get("publisher") or _source_from_url(url)),
                published_at=published,
                snippet=str(item.get("summary") or "").strip(),
                search_provider="yfinance",
            ))
        return articles

    return await asyncio.to_thread(_blocking)


# ---------------------------------------------------------------------------
# Orchestrated per-company search
# ---------------------------------------------------------------------------

class SearchOutcome(BaseModel):
    company: str
    ticker: str
    articles: list[Article]
    providers_used: list[str]
    fallback_used: bool = False
    notes: str = ""


def _stamp(articles: list[Article], company: Company) -> None:
    for a in articles:
        a.company = company.name
        a.ticker = company.ticker


async def search_company_news(
    company: Company,
    settings: Settings,
    cache: CacheManager | None = None,
    max_results: int | None = None,
) -> SearchOutcome:
    """Run the cost-aware search cascade for one company.

    Never raises for search-provider problems: worst case the outcome carries
    whatever (possibly empty) article list could be assembled, with notes.
    """
    max_results = max_results or settings.max_articles_per_company
    min_useful = settings.min_useful_search_results
    cache_key = f"{company.ticker}:{query_builder.build_primary_query(company)}"

    if cache:
        hit = cache.search.get("search", cache_key)
        if hit:
            log.info("[%s] using cached search results (%d articles)", company.ticker, len(hit))
            return SearchOutcome(**hit)

    articles: list[Article] = []
    providers: list[str] = []
    fallback_used = False
    notes: list[str] = []

    # --- primary: SearXNG ----------------------------------------------------
    primary_q = query_builder.build_primary_query(company)
    try:
        primary = await searxng_search(primary_q, settings, max_results=max_results)
        _stamp(primary, company)
        articles.extend(primary)
        providers.append("searxng")
        log.info("[%s] SearXNG primary: %d results", company.ticker, len(primary))
    except SearchError as exc:
        notes.append(f"SearXNG primary failed: {exc}")
        log.warning("[%s] %s", company.ticker, exc)

    # --- refined SearXNG query when results are insufficient -----------------
    if len(articles) < min_useful:
        for refined_q in query_builder.build_refined_queries(company):
            try:
                extra = await searxng_search(refined_q, settings, max_results=max_results)
                _stamp(extra, company)
                before = len(articles)
                articles = dedup_by_url(articles + extra)
                log.info("[%s] SearXNG refined: +%d (%d total)",
                         company.ticker, len(articles) - before, len(articles))
                if len(articles) >= min_useful:
                    break
            except SearchError as exc:
                notes.append(f"SearXNG refined failed: {exc}")
                log.warning("[%s] %s", company.ticker, exc)

    # --- fallback: You.com (only when still insufficient) --------------------
    if len(articles) < min_useful:
        if settings.you_api_key:
            try:
                fb = await youcom_search(primary_q, settings, max_results=max_results)
                _stamp(fb, company)
                articles = dedup_by_url(articles + fb)
                providers.append("you.com")
                fallback_used = True
                log.info("[%s] You.com fallback: %d total", company.ticker, len(articles))
            except SearchError as exc:
                notes.append(f"You.com fallback failed: {exc}")
                log.warning("[%s] %s", company.ticker, exc)
        else:
            notes.append("You.com fallback skipped: YOU_API_KEY not configured")

    # --- supplementary: yfinance headlines -----------------------------------
    yf_articles = await yfinance_news(company, max_results=max_results)
    if yf_articles:
        providers.append("yfinance")
    articles = dedup_by_url(articles + yf_articles)

    # --- deterministic recency filter (before anything expensive) ------------
    before_filter = len(articles)
    articles = [a for a in articles if _recent_enough(a.published_at, settings.news_lookback_days)]
    if len(articles) < before_filter:
        log.info("[%s] recency filter: %d -> %d", company.ticker, before_filter, len(articles))

    outcome = SearchOutcome(
        company=company.name,
        ticker=company.ticker,
        articles=articles[: max_results * 2],  # headroom for dedup/crawl stages
        providers_used=sorted(set(providers)),
        fallback_used=fallback_used,
        notes="; ".join(notes),
    )
    if cache:
        cache.search.set("search", cache_key, outcome.model_dump(mode="json"))
    return outcome


# ---------------------------------------------------------------------------
# Query-driven topic search
# ---------------------------------------------------------------------------

import re as _re

TIME_RANGE_DAYS = {"day": 1, "week": 7, "month": 31, "year": 365}
VALID_TIME_RANGES = tuple(TIME_RANGE_DAYS)


def normalize_query(query: str) -> str:
    """Canonical form for cache keys: trimmed, single-spaced, lowercased."""
    return _re.sub(r"\s+", " ", query.strip().lower())


def build_topic_queries(query: str, limit: int = 3) -> list[str]:
    """Small deterministic query expansion for a user topic.

    The raw query always goes first; at most ``limit`` variants are produced.
    No LLM call — cheap, explainable, and bounded.
    """
    q = query.strip()
    lowered = q.lower()
    variants = [q]
    if "latest" not in lowered and "recent" not in lowered:
        variants.append(f"{q} latest news")
    if "news" not in lowered:
        variants.append(f"{q} news developments")
    return variants[:limit]


class TopicSearchOutcome(BaseModel):
    query: str
    queries_used: list[str] = Field(default_factory=list)
    articles: list[Article]
    providers_used: list[str] = Field(default_factory=list)
    fallback_used: bool = False
    fresh_search: bool = False
    from_cache: bool = False
    time_range: str = "month"
    notes: str = ""


def topic_cache_key(query: str, time_range: str, max_results: int) -> str:
    """Cache key namespaced by the *normalized query* plus search options.

    Different queries never share an entry; neither do different time ranges
    or result limits for the same query.
    """
    return f"topic:{normalize_query(query)}:{time_range}:{max_results}"


async def search_topic(
    query: str,
    settings: Settings,
    cache: CacheManager | None = None,
    *,
    max_results: int = 20,
    time_range: str = "month",
    fresh: bool = False,
) -> TopicSearchOutcome:
    """Query-driven web search: SearXNG primary, You.com fallback when needed.

    Never raises for search-provider problems: worst case the outcome carries
    whatever articles could be assembled, with notes explaining what failed.
    ``fresh=True`` bypasses cached search results for this request (fresh
    results are still written to the cache for later reuse).
    """
    if time_range not in VALID_TIME_RANGES:
        raise ValueError(f"Unsupported time_range: {time_range!r} "
                         f"(expected one of {VALID_TIME_RANGES})")
    query = query.strip()
    if not query:
        raise ValueError("Query must not be empty")

    cache_key = topic_cache_key(query, time_range, max_results)
    if cache and not fresh:
        hit = cache.search.get("search", cache_key)
        if hit:
            log.info("[topic] using cached search results for %r (%d articles)",
                     query[:60], len(hit.get("articles", [])))
            return TopicSearchOutcome(**{**hit, "from_cache": True, "fresh_search": False})

    min_useful = settings.min_useful_search_results
    queries = build_topic_queries(query)
    articles: list[Article] = []
    providers: list[str] = []
    fallback_used = False
    notes: list[str] = []
    queries_used: list[str] = []

    for i, q in enumerate(queries):
        try:
            found = await searxng_search(q, settings, max_results=max_results,
                                         time_range=time_range)
            queries_used.append(q)
            before = len(articles)
            articles = dedup_by_url(articles + found)
            providers.append("searxng")
            log.info("[topic] SearXNG %r: +%d (%d total)", q[:60],
                     len(articles) - before, len(articles))
            if len(articles) >= min_useful:
                break  # enough results; skip further expansion (cost discipline)
        except SearchError as exc:
            notes.append(f"SearXNG failed for {q!r}: {exc}")
            log.warning("[topic] %s", exc)

    # Fallback: You.com on the raw query, only when still insufficient.
    if len(articles) < min_useful:
        if settings.you_api_key:
            try:
                fb = await youcom_search(query, settings, max_results=max_results)
                articles = dedup_by_url(articles + fb)
                providers.append("you.com")
                fallback_used = True
                log.info("[topic] You.com fallback: %d total", len(articles))
            except SearchError as exc:
                notes.append(f"You.com fallback failed: {exc}")
                log.warning("[topic] %s", exc)
        else:
            notes.append("You.com fallback skipped: YOU_API_KEY not configured")

    # Deterministic recency filter governed by the requested time range.
    cutoff_days = TIME_RANGE_DAYS[time_range]
    before_filter = len(articles)
    articles = [a for a in articles if _recent_enough(a.published_at, cutoff_days)]
    if len(articles) < before_filter:
        log.info("[topic] recency filter (%s): %d -> %d",
                 time_range, before_filter, len(articles))

    outcome = TopicSearchOutcome(
        query=query,
        queries_used=queries_used,
        articles=articles[: max_results * 2],  # headroom for dedup/crawl stages
        providers_used=sorted(set(providers)),
        fallback_used=fallback_used,
        fresh_search=fresh,
        from_cache=False,
        time_range=time_range,
        notes="; ".join(notes),
    )
    if cache:
        cache.search.set("search", cache_key, outcome.model_dump(mode="json"))
    return outcome
