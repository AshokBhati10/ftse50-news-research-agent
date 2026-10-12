from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import httpx

from .cache import Cache
from .models import SearchHit, SearchQuery, SearchResponse
from .urls import hostname, normalize_url

log = logging.getLogger(__name__)


class SearxError(RuntimeError):
    pass


class SearxClient:
    """Async client for a private SearXNG instance: GET /search?format=json."""

    def __init__(
        self,
        base_url: str,
        cache: Cache | None = None,
        *,
        search_ttl: int = 6 * 3600,
        news_ttl: int = 900,
        timeout: float = 25.0,
        max_retries: int = 2,
        concurrency: int = 3,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        self._cache = cache
        self._search_ttl = search_ttl
        self._news_ttl = news_ttl
        self._max_retries = max_retries
        self._sem = asyncio.Semaphore(concurrency)  # be polite to upstream engines
        self._engines: list[str] | None = None

    async def close(self) -> None:
        await self._http.aclose()

    def _params(self, sq: SearchQuery, pageno: int, language: str) -> dict[str, str]:
        params: dict[str, str] = {
            "q": sq.q,
            "format": "json",
            "pageno": str(pageno),
            "language": language,
            "safesearch": "0",
        }
        if sq.engines:
            params["engines"] = ",".join(sq.engines)
        else:
            params["categories"] = sq.categories
        if sq.time_range:
            params["time_range"] = sq.time_range
        return params

    async def _get(self, params: dict[str, str]) -> dict[str, Any]:
        delay = 1.0
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._http.get("/search", params=params)
                if resp.status_code == 403:
                    raise SearxError("403 from SearXNG: is 'json' listed in search.formats in settings.yml?")
                if resp.status_code == 429:
                    raise SearxError("429 from SearXNG: the limiter is on; turn it off or pass_ip the app network")
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt == self._max_retries:
                    raise SearxError(f"SearXNG request failed: {exc}") from exc
                log.warning("searxng retry %s after %s", attempt + 1, exc)
                await asyncio.sleep(delay)
                delay *= 2
        raise SearxError("unreachable")

    async def search(self, sq: SearchQuery, *, pageno: int = 1, language: str = "en") -> SearchResponse:
        params = self._params(sq, pageno, language)
        ttl = self._news_ttl if sq.categories == "news" or sq.time_range == "day" else self._search_ttl
        key = Cache.key("sx", params)

        data: dict[str, Any] | None = await self._cache.get_json(key) if self._cache else None
        if data is None:
            async with self._sem:
                data = await self._get(params)
            # Don't cache empty answers: they are usually a temporary upstream block.
            if self._cache and data.get("results"):
                await self._cache.set_json(key, data, ttl)

        hits = [self._to_hit(r, sq.q) for r in data.get("results", []) if r.get("url")]
        unresponsive = [(str(e[0]), str(e[1])) for e in data.get("unresponsive_engines", [])]
        if unresponsive:
            log.info("unresponsive engines for %r: %s", sq.q, unresponsive)
        return SearchResponse(
            query=sq,
            hits=hits,
            suggestions=list(data.get("suggestions", [])),
            unresponsive_engines=unresponsive,
        )

    @staticmethod
    def _to_hit(r: dict[str, Any], query: str) -> SearchHit:
        return SearchHit(
            url=r["url"],
            canonical_url=normalize_url(r["url"]),
            title=(r.get("title") or "").strip(),
            snippet=(r.get("content") or "").strip(),
            engine=r.get("engine", ""),
            engines=list(r.get("engines") or [r.get("engine", "")]),
            score=float(r.get("score") or 0.0),
            category=r.get("category", ""),
            published=r.get("publishedDate"),
            query=query,
        )

    async def known_engines(self) -> list[str] | None:
        """Engine names the instance reports on /config (its settings.yml is the single source of truth).

        Returns None when the instance cannot be asked, so callers ignore engine hints rather than guess.
        """
        if self._engines is not None:
            return self._engines
        try:
            resp = await self._http.get("/config")
            resp.raise_for_status()
            self._engines = [str(e["name"]) for e in resp.json().get("engines", []) if e.get("enabled", True)]
        except (httpx.HTTPError, ValueError, KeyError):
            return None
        return self._engines

    async def engine_errors(self) -> dict[str, Any]:
        """GET /stats/errors: per-engine error stats (useful for a health dashboard)."""
        resp = await self._http.get("/stats/errors")
        resp.raise_for_status()
        return resp.json()

    async def healthy(self) -> bool:
        try:
            return (await self._http.get("/healthz")).status_code == 200
        except httpx.HTTPError:
            return False


def hit_to_article(hit: SearchHit, company: str = "", ticker: str = "") -> Any:
    """Bridge to the existing FTSE pipeline: convert a SearXNG hit into `utils.models.Article`.

    The pipeline's own SearXNG code (nodes/search.py) is left untouched; this lets results from
    this client feed the same downstream extraction / analysis / reporting steps. It needs the
    top-level repo on sys.path, so it is only available when run next to the pipeline (not inside
    the research-stack container); the import is therefore done lazily.
    """
    from utils.models import Article  # noqa: PLC0415  (top-level FTSE pipeline)

    published: datetime | None = None
    if hit.published:
        try:
            published = datetime.fromisoformat(hit.published.replace("Z", "+00:00"))
        except ValueError:
            published = None
    return Article(
        company=company,
        ticker=ticker,
        title=hit.title,
        url=hit.url,
        source=hostname(hit.url),
        published_at=published,
        snippet=hit.snippet,
        search_provider="searxng",
    )
