"""Node 3a — Article extraction.

Primary: Crawl4AI (handles JS-heavy pages, returns clean Markdown).
Fallback: lightweight httpx + lxml reader that pulls <article>/<main> text —
          deliberately *not* a second heavy framework, just a safety net.

Failure policy (never crash the run):
  Crawl4AI fails -> bounded retry -> fallback extractor -> snippet-only ->
  mark ``extraction_status`` and continue.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

import httpx
from lxml import html as lxml_html

from config.settings import Settings
from utils.cache import CacheManager
from utils.models import Article
from utils.rate_limiter import retry_async

log = logging.getLogger(__name__)

_CRAWL_SEMAPHORE = asyncio.Semaphore(3)  # re-bound from settings at runtime


# ---------------------------------------------------------------------------
# Fallback extractor (plain httpx + lxml — no extra framework)
# ---------------------------------------------------------------------------

_BLOCK_TAGS = (
    "//article//p | //main//p | "
    "//div[contains(@class,'article')]//p | //div[contains(@class,'content')]//p | "
    "//div[contains(@class,'post')]//p"
)


def fallback_extract(url: str, html: str, min_chars: int = 400) -> str | None:
    """Best-effort readable-text extraction from raw HTML."""
    try:
        doc = lxml_html.fromstring(html)
    except Exception:
        return None
    for sel in (_BLOCK_TAGS, "//p"):
        paras = [
            p.text_content().strip()
            for p in doc.xpath(sel)
            if len(p.text_content().strip()) > 40
        ]
        text = "\n\n".join(paras)
        if len(text) >= min_chars:
            return text
    return None


async def _fallback_fetch(url: str, settings: Settings) -> str | None:
    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; FTSE50NewsAgent/1.0)"},
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype:
                return None
            return fallback_extract(url, resp.text, settings.extraction_min_chars)
    except Exception as exc:  # noqa: BLE001 — fallback must not raise
        log.debug("Fallback extraction failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Crawl4AI primary path (lazy import — keeps `pytest` light without browsers)
# ---------------------------------------------------------------------------

async def _crawl4ai_extract(url: str, settings: Settings) -> str | None:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

    browser_cfg = BrowserConfig(headless=True, verbose=False)
    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=int(settings.crawl_timeout_s * 1000),
        word_count_threshold=50,
        excluded_tags=["nav", "header", "footer", "aside", "form"],
    )
    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        result = await crawler.arun(url=url, config=run_cfg)
    if not result or not result.success:
        raise RuntimeError(f"crawl4ai unsuccessful: {getattr(result, 'error_message', 'unknown')}")
    text = (result.markdown or "").strip()
    return text if len(text) >= settings.extraction_min_chars else None


# ---------------------------------------------------------------------------
# Per-article orchestration
# ---------------------------------------------------------------------------

def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:32]


async def extract_article(
    article: Article,
    settings: Settings,
    cache: CacheManager | None = None,
    semaphore: asyncio.Semaphore | None = None,
) -> Article:
    """Extract full text for one article, applying the failure policy."""
    sem = semaphore or _CRAWL_SEMAPHORE
    key = _cache_key(article.url)

    if cache:
        hit = cache.extraction.get("extraction", key)
        if hit:
            article.content = hit.get("content")
            article.extraction_status = hit.get("status", "ok")
            article.evidence_excerpt = (article.content or "")[:500]
            return article

    async with sem:
        content: str | None = None
        status = "failed"

        async def _try_crawl() -> str:
            text = await asyncio.wait_for(
                _crawl4ai_extract(article.url, settings),
                timeout=settings.crawl_timeout_s + 15,
            )
            if not text:
                raise RuntimeError("crawl4ai returned too little text")
            return text

        try:
            content = await retry_async(
                _try_crawl,
                max_retries=settings.crawl_max_retries,
                base_delay_s=2.0,
                retry_on=(Exception,),
                description=f"crawl {article.url}",
            )
            status = "ok"
        except Exception as exc:  # noqa: BLE001 — bounded, then fall through
            log.warning("Crawl4AI failed for %s (%s); trying fallback extractor", article.url, exc)
            content = await _fallback_fetch(article.url, settings)
            status = "ok" if content else "failed"

        if content:
            article.content = content
            article.extraction_status = status
            article.evidence_excerpt = content[:500]
        elif article.snippet:
            article.extraction_status = "snippet_only"
            article.evidence_excerpt = article.snippet[:500]
            log.info("Using snippet-only for %s", article.url)
        else:
            article.extraction_status = "failed"

    if cache:
        cache.extraction.set("extraction", key, {
            "content": article.content,
            "status": article.extraction_status,
        })
    return article


async def extract_articles(
    articles: list[Article],
    settings: Settings,
    cache: CacheManager | None = None,
) -> list[Article]:
    """Extract a batch concurrently with a bounded crawl semaphore."""
    sem = asyncio.Semaphore(settings.max_crawl_concurrency)
    results = await asyncio.gather(
        *(extract_article(a, settings, cache, sem) for a in articles),
        return_exceptions=False,
    )
    ok = sum(1 for a in results if a.extraction_status == "ok")
    log.info("Extraction: %d/%d ok (rest: snippet_only/failed, pipeline continues)",
             ok, len(results))
    return list(results)
