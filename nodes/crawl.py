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
import copy
import hashlib
import json
import logging
import re
from datetime import datetime, timezone

import httpx
from dateutil import parser as date_parser
from lxml import html as lxml_html

from config.settings import Settings
from utils.cache import CacheManager
from utils.models import Article
from utils.net_guard import (
    UnsafeURLError,
    assert_public_url,
    assert_public_url_resolved,
)
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


# ---------------------------------------------------------------------------
# Page metadata: publication date, title, site name, canonical URL
# ---------------------------------------------------------------------------

_PUBLISHED_XPATHS = (
    '//meta[@property="article:published_time"]/@content',
    '//meta[@property="og:published_time"]/@content',
    '//meta[@name="pubdate"]/@content',
    '//meta[@name="publishdate"]/@content',
    '//meta[@name="publish-date"]/@content',
    '//meta[@name="date"]/@content',
    '//meta[@name="dc.date"]/@content',
    '//meta[@name="dc.date.issued"]/@content',
    '//meta[@name="datePublished"]/@content',
    '//meta[@itemprop="datePublished"]/@content',
    '//meta[@name="parsely-pub-date"]/@content',
)
_SOURCE_XPATHS = (
    '//meta[@property="og:site_name"]/@content',
    '//meta[@name="twitter:site"]/@content',
    '//meta[@name="publisher"]/@content',
    '//meta[@name="application-name"]/@content',
)


# Phrases that indicate the page body is behind a paywall/registration wall.
# Deliberately narrow: only strong "wall" calls-to-action count. Ordinary
# mentions of "subscribe"/"premium"/"paywall" in prose or nav links must NOT
# trigger a false positive (detection only matters when no body was extracted).
_PAYWALL_RE = re.compile(
    r"subscribe (?:now )?to (?:continue|keep) reading|"
    r"subscribe to (?:read|unlock|access) (?:the )?(?:full|rest)"
    r" (?:article|story|version|content|report)|"
    r"sign in to (?:continue|keep) reading|"
    r"sign in to read (?:the )?(?:full|rest)"
    r" (?:article|story|version|content|report)|"
    r"register to (?:continue|keep) reading|"
    r"this (?:article|content|story) is (?:for|available to) "
    r"(?:subscribers|members)|"
    r"become a (?:subscriber|member) to "
    r"(?:read|unlock|access|continue|keep)|"
    r"to continue reading,? (?:please )?(?:subscribe|sign in|register)|"
    r"(?:subscribers|members)[ -]only",
    re.I,
)


def detect_paywall(text: str) -> bool:
    """Heuristic: does this page text signal a paywall/registration wall?"""
    return bool(_PAYWALL_RE.search(text or ""))


def _visible_text(doc) -> str:
    """Body text with script/style/noscript/template removed (paywall scan)."""
    try:
        clean = copy.deepcopy(doc)
        for bad in clean.xpath("//script | //style | //noscript | //template"):
            bad.drop_tree()
        return clean.text_content()
    except Exception:  # noqa: BLE001
        return ""


def _parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        dt = date_parser.parse(str(value))
    except (ValueError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _jsonld_nodes(data: object):
    """Yield candidate article objects from a JSON-LD document."""
    if isinstance(data, list):
        for item in data:
            yield from _jsonld_nodes(item)
    elif isinstance(data, dict):
        if isinstance(data.get("@graph"), (list, dict)):
            yield from _jsonld_nodes(data["@graph"])
        else:
            yield data


_ARTICLE_LD_TYPES = {
    "newsarticle", "reportagenewsarticle", "analysisnewsarticle",
    "opinionnewsarticle", "blogposting", "techarticle", "article",
}


def _jsonld_rank(node: dict) -> int:
    """Sort key: article-typed JSON-LD nodes before wrapper nodes."""
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    lowered = {str(t).lower() for t in types if t}
    return 0 if lowered & _ARTICLE_LD_TYPES else 1


def _first_xpath(doc, xpaths: tuple[str, ...]) -> str:
    for xp in xpaths:
        try:
            vals = doc.xpath(xp)
        except Exception:  # noqa: BLE001 — malformed HTML must not raise
            continue
        if vals and str(vals[0]).strip():
            return str(vals[0]).strip()
    return ""


def extract_metadata(html: str, url: str = "") -> dict:
    """Best-effort page metadata from HTML. Never raises.

    Returns ``{"title", "published_at", "source", "paywalled"}`` where
    ``published_at`` is a timezone-aware datetime or None. JSON-LD is preferred,
    then OpenGraph/standard meta tags, then a ``<time datetime>`` element.
    """
    meta: dict = {"title": "", "published_at": None, "source": "",
                  "paywalled": False}
    try:
        doc = lxml_html.fromstring(html or "")
    except Exception:  # noqa: BLE001
        return meta
    meta["paywalled"] = detect_paywall(_visible_text(doc))

    # 1. JSON-LD (most structured / reliable when present). Collect all nodes
    # first, then prefer article-typed nodes over WebSite/WebPage wrappers so a
    # @graph document yields the article's own headline/date.
    ld_nodes: list[dict] = []
    for script in doc.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            data = json.loads(script)
        except (ValueError, TypeError):
            continue
        ld_nodes.extend(n for n in _jsonld_nodes(data) if isinstance(n, dict))
    for node in sorted(ld_nodes, key=_jsonld_rank):
        if meta["published_at"] is None:
            meta["published_at"] = _parse_dt(
                node.get("datePublished") or node.get("dateCreated")
                or node.get("dateModified"))
        if not meta["title"]:
            meta["title"] = str(node.get("headline")
                                or node.get("name") or "").strip()
        publisher = node.get("publisher")
        if not meta["source"] and isinstance(publisher, dict):
            meta["source"] = str(publisher.get("name") or "").strip()
        elif not meta["source"] and isinstance(publisher, str):
            meta["source"] = publisher.strip()

    # 2. Meta tags.
    if meta["published_at"] is None:
        meta["published_at"] = _parse_dt(_first_xpath(doc, _PUBLISHED_XPATHS))
    if not meta["published_at"]:
        times = doc.xpath("//time/@datetime")
        if times:
            meta["published_at"] = _parse_dt(times[0])

    if not meta["source"]:
        meta["source"] = _first_xpath(doc, _SOURCE_XPATHS)
    if not meta["title"]:
        titles = doc.xpath("//title/text()")
        if titles:
            meta["title"] = str(titles[0]).strip()
    return meta


def _merge_meta(primary: dict, secondary: dict) -> dict:
    """Merge metadata dicts, preferring the first non-empty value."""
    out = dict(primary or {})
    for key, value in (secondary or {}).items():
        if not out.get(key):
            out[key] = value
    return out


def _apply_metadata(article: Article, meta: dict) -> None:
    """Fill missing article fields from page metadata (never overwrite).

    Accepts either a datetime or an ISO string for ``published_at`` (the cache
    stores the string form).
    """
    if not meta:
        return
    published = meta.get("published_at")
    if article.published_at is None and published:
        article.published_at = (published if isinstance(published, datetime)
                                else _parse_dt(published))
    if not article.title and meta.get("title"):
        article.title = str(meta["title"])
    if not article.source and meta.get("source"):
        article.source = str(meta["source"])


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


async def _fallback_fetch(url: str, settings: Settings) -> tuple[str | None, dict]:
    """Fetch with per-hop SSRF validation; redirects are followed manually.

    Returns ``(text, meta)`` — ``text`` is None when nothing usable was
    extracted, and ``meta`` carries any publication date/title/site the page
    exposed. Never raises.
    """
    try:
        current = url
        async with httpx.AsyncClient(
            timeout=20.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; FTSE50NewsAgent/1.0)"},
            follow_redirects=False,
        ) as client:
            for _hop in range(5):
                await assert_public_url_resolved(current)
                resp = await client.get(current)
                if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        break
                    current = _join_url(current, location)
                    continue
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "")
                if "html" not in ctype:
                    return None, {}
                meta = extract_metadata(resp.text, current)
                text = fallback_extract(current, resp.text,
                                        settings.extraction_min_chars)
                return text, meta
            log.warning("Redirect limit reached fetching %s", url)
            return None, {}
    except Exception as exc:  # noqa: BLE001 — fallback must not raise
        log.debug("Fallback extraction failed for %s: %s", url, exc)
        return None, {}


def _join_url(base: str, location: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, location)


# ---------------------------------------------------------------------------
# Crawl4AI primary path (lazy import — keeps `pytest` light without browsers)
# ---------------------------------------------------------------------------

async def _crawl4ai_extract(url: str, settings: Settings) -> tuple[str | None, dict]:
    """Crawl4AI extraction. Returns ``(text, meta)``.

    Raises only on hard crawler failure; a thin/empty extraction returns
    ``(None, meta)`` so the caller can fall back without losing metadata.
    """
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
    # Redirected to a non-public host? Discard rather than expose it.
    final_url = getattr(result, "url", "") or url
    try:
        await assert_public_url_resolved(final_url)
    except UnsafeURLError as exc:
        raise RuntimeError(f"blocked: redirected to a non-public URL: {exc}") from exc

    html = getattr(result, "html", "") or ""
    meta = extract_metadata(html, final_url) if html else {
        "title": "", "published_at": None, "source": "", "paywalled": False}
    r_meta = getattr(result, "metadata", None) or {}
    if isinstance(r_meta, dict):
        if not meta["title"]:
            meta["title"] = str(r_meta.get("title") or "").strip()
        if not meta["source"]:
            meta["source"] = str(r_meta.get("site_name")
                                 or r_meta.get("publisher") or "").strip()
        if not meta["published_at"]:
            meta["published_at"] = _parse_dt(
                r_meta.get("published_time") or r_meta.get("publishedTime"))

    text = (result.markdown or "").strip()
    meta["paywalled"] = bool(meta.get("paywalled")) or detect_paywall(text)
    return (text if len(text) >= settings.extraction_min_chars else None), meta


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

    # SSRF guard: never fetch private/loopback/link-local targets. Fail the
    # article (not the run): callers aggregate concurrently per batch.
    try:
        assert_public_url(article.url)
        await assert_public_url_resolved(article.url)
    except UnsafeURLError as exc:
        log.warning("Blocked fetch of non-public URL %s: %s", article.url, exc)
        article.extraction_status = "failed"
        return article

    if cache:
        hit = cache.extraction.get("extraction", key)
        if hit:
            article.content = hit.get("content")
            article.extraction_status = hit.get("status", "ok")
            article.evidence_excerpt = (article.content
                                        or article.snippet or "")[:500]
            _apply_metadata(article, hit.get("meta") or {})
            return article

    async with sem:
        content: str | None = None
        meta: dict = {}
        status = "failed"

        async def _try_crawl() -> tuple[str | None, dict]:
            return await asyncio.wait_for(
                _crawl4ai_extract(article.url, settings),
                timeout=settings.crawl_timeout_s + 15,
            )

        try:
            content, meta = await retry_async(
                _try_crawl,
                max_retries=settings.crawl_max_retries,
                base_delay_s=2.0,
                retry_on=(Exception,),
                description=f"crawl {article.url}",
            )
            status = "ok" if content else "failed"
        except Exception as exc:  # noqa: BLE001 — bounded, then fall through
            log.warning("Crawl4AI failed for %s (%s); trying fallback extractor",
                        article.url, exc)

        if not content:
            content, fb_meta = await _fallback_fetch(article.url, settings)
            meta = _merge_meta(meta, fb_meta)
            if content:
                status = "ok"

        _apply_metadata(article, meta)
        if content:
            article.content = content
            article.extraction_status = status
            article.evidence_excerpt = content[:500]
        elif meta.get("paywalled"):
            # No body text and the page is paywalled: keep any search snippet
            # as evidence and record the reason instead of a generic failure.
            article.extraction_status = "paywalled"
            article.evidence_excerpt = article.snippet[:500]
            log.info("Paywalled page; using search snippet for %s", article.url)
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
            "meta": {
                "title": article.title,
                "source": article.source,
                "published_at": (article.published_at.isoformat()
                                 if article.published_at else None),
            },
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
    paywalled = sum(1 for a in results if a.extraction_status == "paywalled")
    log.info("Extraction: %d/%d ok, %d paywalled (rest snippet_only/failed; "
             "pipeline continues)", ok, len(results), paywalled)
    return list(results)
