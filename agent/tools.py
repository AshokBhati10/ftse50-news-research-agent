"""Agent tools: registry, validated executor, and tool implementations.

The model may only *request* tool calls; :class:`ToolExecutor` validates the
tool name and arguments against Pydantic schemas and runs the implementation.
The model never executes code or touches the network directly.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from config.settings import Settings
from nodes.search import (
    VALID_TIME_RANGES,
    SearchError,
    dedup_by_url,
    searxng_search,
    youcom_search,
)
from utils.cache import CacheManager
from utils.models import Article, PERMITTED_TOOLS
from utils.net_guard import UnsafeURLError, assert_public_url
from utils.source_quality import is_section_page, rank_by_source_quality
from utils.story_dedup import dedup_stories

log = logging.getLogger(__name__)

_STOPWORDS = frozenset(
    "a an the and or of in on to for with is are was were be by at from as it "
    "its this that these those what who when where which how why did does do "
    "s".split()
)


# ---------------------------------------------------------------------------
# Tool argument schemas
# ---------------------------------------------------------------------------

class WebSearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=5, ge=1, le=10)
    time_range: str = Field(default="month")

    @field_validator("time_range")
    @classmethod
    def _valid_range(cls, v: str) -> str:
        if v not in VALID_TIME_RANGES:
            raise ValueError(f"time_range must be one of {VALID_TIME_RANGES}")
        return v

    @field_validator("query")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query must not be blank")
        return v


class FetchArticleArgs(BaseModel):
    url: str = Field(min_length=1, max_length=2000)

    @field_validator("url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        v = v.strip()
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError("url must be absolute http(s)")
        # SSRF guard: http(s) only, public host/IP, no credentials, standard port.
        try:
            assert_public_url(v)
        except UnsafeURLError as exc:
            raise ValueError(f"url blocked: {exc}") from exc
        return v


# ---------------------------------------------------------------------------
# Search quality: landing-page filtering + relevance ranking
# ---------------------------------------------------------------------------

def _is_homepage(url: str) -> bool:
    try:
        return urlparse(url).path.rstrip("/") in ("", "/")
    except Exception:
        return False


def _query_terms(query: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", query.lower())
            if t not in _STOPWORDS and len(t) > 2]


def _overlap_ratio(query: str, title: str, snippet: str) -> float:
    terms = _query_terms(query)
    if not terms:
        return 0.0
    haystack = f"{title} {snippet}".lower()
    hits = sum(1 for t in terms if t in haystack)
    return hits / len(terms)


def _recency_bonus(a: Article) -> float:
    """Prefer dated, recent results; undated items get no bonus."""
    if a.published_at is None:
        return 0.0
    published = a.published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - published).total_seconds() / 86400.0
    if age_days <= 2:
        return 0.20
    if age_days <= 14:
        return 0.12
    if age_days <= 45:
        return 0.06
    return 0.0


def _search_score(a: Article, query: str) -> float:
    """Relevance + reliability + recency, with hub/homepage penalties.

    - source quality (deterministic domain tiers)
    - query-term overlap (title + snippet)
    - recency bonus for dated items (news relevance)
    - full-text bonus when content was extracted
    - penalties for homepages and undated section/index pages
    """
    score = a.source_quality
    score += 0.5 * _overlap_ratio(query, a.title, a.snippet)
    score += _recency_bonus(a)
    if (a.content or "").strip():
        score += 0.10
    if _is_homepage(a.url):
        score -= 0.40
    if a.published_at is None and is_section_page(a.title, a.snippet):
        score -= 0.30
    return score


def filter_and_rank(articles: list[Article], query: str,
                    max_results: int) -> list[Article]:
    """Drop unusable results; de-duplicate stories; rank by quality+relevance.

    - Drops: empty titles, or no snippet and no extracted content.
    - Collapses near-duplicate/syndicated headlines so one story does not fill
      the result set (the highest-ranked copy survives).
    - Deprioritizes (not silently drops): homepage/category URLs and undated
      section pages, which rarely answer specific questions.
    """
    if max_results <= 0:
        return []
    usable = [a for a in articles
              if a.title.strip() and (a.snippet.strip() or (a.content or "").strip())]
    dropped = len(articles) - len(usable)
    if dropped:
        log.info("filter_and_rank: dropped %d unusable results", dropped)

    usable.sort(key=lambda a: _search_score(a, query), reverse=True)
    distinct = dedup_stories(
        usable, title_getter=lambda a: a.title, threshold=0.8)
    if len(distinct) < len(usable):
        log.info("filter_and_rank: collapsed %d near-duplicate story result(s)",
                 len(usable) - len(distinct))
    return distinct[:max_results]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

class SearchTool:
    """Web search via SearXNG (primary) with optional You.com fallback."""

    def __init__(self, settings: Settings, cache: CacheManager | None = None,
                 fresh: bool = False) -> None:
        self.settings = settings
        self.cache = cache
        self.fresh = fresh

    async def run(self, args: WebSearchArgs) -> dict[str, Any]:
        from nodes.search import topic_cache_key

        cache_key = topic_cache_key(args.query, args.time_range, args.max_results)
        if self.cache and not self.fresh:
            hit = self.cache.search.get("search", cache_key)
            if hit:
                log.info("web_search: cached results for %r", args.query[:60])
                return {**hit, "from_cache": True}

        articles: list[Article] = []
        providers: list[str] = []
        notes: list[str] = []
        try:
            articles = await searxng_search(
                args.query, self.settings,
                max_results=args.max_results, time_range=args.time_range)
            providers.append("searxng")
        except SearchError as exc:
            notes.append(f"SearXNG failed: {exc}")
            log.warning("web_search: %s", exc)
        if not articles and self.settings.you_api_key:
            try:
                articles = await youcom_search(
                    args.query, self.settings, max_results=args.max_results)
                providers.append("you.com")
            except SearchError as exc:
                notes.append(f"You.com fallback failed: {exc}")
                log.warning("web_search: %s", exc)
        articles = dedup_by_url(articles)
        # Score domains before ranking so the source-quality term is real
        # (searchers don't populate source_quality themselves).
        rank_by_source_quality(articles)
        articles = filter_and_rank(articles, args.query, args.max_results)
        result = {
            "ok": bool(articles),
            "results": [
                {
                    "title": a.title,
                    "url": a.url,
                    "source": a.source,
                    "published_at": a.published_at.isoformat() if a.published_at else None,
                    "snippet": a.snippet[:500],
                    "source_quality": round(a.source_quality, 3),
                    "is_homepage": _is_homepage(a.url),
                }
                for a in articles
            ],
            "providers": providers,
            "notes": notes,
            "from_cache": False,
        }
        if self.cache:
            self.cache.search.set("search", cache_key, result)
        return result


class ArticleExtractor:
    """Fetch and extract the full text of a single article URL."""

    def __init__(self, settings: Settings, cache: CacheManager | None = None) -> None:
        self.settings = settings
        self.cache = cache

    async def run(self, args: FetchArticleArgs) -> dict[str, Any]:
        from nodes.crawl import extract_article

        article = Article(company="", ticker="", title="", url=args.url,
                          snippet="", search_provider="agent")
        try:
            article = await extract_article(article, self.settings, self.cache)
        except Exception as exc:  # noqa: BLE001 — extraction must not kill the run
            log.warning("fetch_article failed for %s: %s", args.url, exc)
            return {"ok": False, "url": args.url,
                    "error": f"Extraction failed: {exc}"}
        text = (article.content or "").strip()
        status = article.extraction_status
        error = None
        if status == "paywalled":
            error = "Article appears paywalled; no full text available."
        ok = status in ("ok", "snippet_only") and bool(text)
        return {
            "ok": ok,
            "url": args.url,
            "error": error,
            "title": article.title or "",
            "published_at": (article.published_at.isoformat()
                             if article.published_at else None),
            "extraction_status": status,
            "content": text[:8000],
            "source": article.source or urlparse(args.url).netloc,
        }


# ---------------------------------------------------------------------------
# Registry + validated executor
# ---------------------------------------------------------------------------

class ToolDefinition(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    name: str
    description: str
    args_model: type[BaseModel]
    instance: Any  # tool object; method resolved at call time (patchable)
    method: str = "run"


class ToolRegistry:
    """The fixed set of tools the agent may use."""

    def __init__(self, settings: Settings,
                 cache: CacheManager | None = None) -> None:
        search_tool = SearchTool(settings)
        extractor = ArticleExtractor(settings, cache)
        self._tools: dict[str, ToolDefinition] = {
            "web_search": ToolDefinition(
                name="web_search",
                description="Search the web for the query. Returns ranked "
                            "results with title, url, source, snippet, date.",
                args_model=WebSearchArgs,
                instance=search_tool,
            ),
            "fetch_article": ToolDefinition(
                name="fetch_article",
                description="Fetch and extract the full text of one article URL.",
                args_model=FetchArticleArgs,
                instance=extractor,
            ),
        }
        assert set(self._tools) <= set(PERMITTED_TOOLS)

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def describe(self, allowed: list[str] | None = None) -> str:
        """Describe tools, optionally restricted to the plan's allowlist."""
        if allowed is None:
            chosen = list(self._tools)
        else:
            chosen = [t for t in allowed if t in self._tools]
        lines = []
        for t in chosen:
            fields = ", ".join(
                f"{n}" for n in self._tools[t].args_model.model_fields)
            lines.append(f"- {t}({fields}): {self._tools[t].description}")
        return "\n".join(lines)


class ToolValidationError(ValueError):
    pass


class ToolExecutor:
    """Validates and executes permitted tool calls, with duplicate suppression."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self._seen: set[str] = set()

    @staticmethod
    def _signature(tool_name: str, args: dict[str, Any]) -> str:
        norm = {k: (v.strip().lower() if isinstance(v, str) else v)
                for k, v in sorted(args.items())}
        return f"{tool_name}:{norm}"

    async def execute(self, tool_name: str, arguments: dict[str, Any],
                      *, allow_duplicate: bool = False) -> dict[str, Any]:
        tool = self.registry.get(tool_name)
        if tool is None:
            raise ToolValidationError(f"Unknown tool: {tool_name!r}. "
                                      f"Permitted: {self.registry.names()}")
        try:
            parsed = tool.args_model(**(arguments or {}))
        except Exception as exc:
            raise ToolValidationError(
                f"Invalid arguments for {tool_name}: {exc}") from exc
        sig = self._signature(tool_name, parsed.model_dump())
        if not allow_duplicate and sig in self._seen:
            log.info("Skipping duplicate tool call: %s", sig[:120])
            return {"ok": False, "duplicate": True,
                    "error": "Identical tool call already executed; "
                             "refine the query or finish."}
        self._seen.add(sig)
        try:
            # Resolve the method at call time so tests can stub tool methods.
            result = await getattr(tool.instance, tool.method)(parsed)
        except Exception as exc:  # noqa: BLE001 — tools fail safely
            log.exception("Tool %s failed", tool_name)
            return {"ok": False, "error": f"Tool {tool_name} failed: {exc}"}
        if isinstance(result, dict):
            return result
        return {"ok": True, "result": result}

    @property
    def call_count(self) -> int:
        return len(self._seen)
