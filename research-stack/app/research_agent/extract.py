from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
import time
from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .cache import Cache
from .models import Document, SearchHit
from .urls import content_hash, hostname

# trafilatura and pymupdf are imported lazily inside the functions that use them, so this module
# imports cleanly even where those libraries are not installed (they are in app/requirements.txt).

log = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024  # skip anything larger than 20 MB
MIN_TEXT_CHARS = 600  # below this, assume the page needs JavaScript and try the browser fallback


# Some networks resolve every name to a "fake IP" (e.g. 198.18.0.0/15 behind a transparent proxy).
# List such ranges, comma-separated, in FETCH_ALLOWED_NETS so the SSRF guard does not block everything.
ALLOWED_NETS = [ipaddress.ip_network(n.strip()) for n in os.getenv("FETCH_ALLOWED_NETS", "").split(",") if n.strip()]


class BlockedURL(httpx.RequestError):
    pass


async def is_public_host(host: str) -> bool:
    """SSRF guard: search results are untrusted, so never fetch private, loopback or link-local addresses
    (that would let a malicious result reach Valkey, Qdrant or your LAN)."""
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global and not any(ip in net for net in ALLOWED_NETS):
            return False
    return True


async def _guard_request(request: httpx.Request) -> None:
    # httpx calls request hooks for every hop, so redirects to internal hosts are blocked too.
    if request.url.scheme not in ("http", "https") or not await is_public_host(request.url.host):
        raise BlockedURL(f"blocked non-public URL: {request.url}", request=request)


class HostThrottle:
    """At most one request per host every `delay` seconds (polite crawling)."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last: dict[str, float] = {}

    async def wait(self, host: str) -> None:
        async with self._locks[host]:
            elapsed = time.monotonic() - self._last.get(host, 0.0)
            if elapsed < self.delay:
                await asyncio.sleep(self.delay - elapsed)
            self._last[host] = time.monotonic()


class Robots:
    """Caches robots.txt per host and answers can_fetch()."""

    def __init__(self, http: httpx.AsyncClient, user_agent: str) -> None:
        self._http = http
        self._ua = user_agent
        self._parsers: dict[str, RobotFileParser | None] = {}

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._parsers:
            try:
                resp = await self._http.get(f"{base}/robots.txt", timeout=8.0)
                if resp.status_code >= 400:
                    self._parsers[base] = None  # no robots.txt -> allowed
                else:
                    rp = RobotFileParser()
                    rp.parse(resp.text.splitlines())
                    self._parsers[base] = rp
            except httpx.HTTPError:
                self._parsers[base] = None
        rp = self._parsers[base]
        return True if rp is None else rp.can_fetch(self._ua, url)


def _trafilatura_extract(html: str, url: str) -> dict[str, Any] | None:
    import trafilatura

    out = trafilatura.extract(
        html,
        url=url,
        output_format="json",
        with_metadata=True,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )
    return json.loads(out) if out else None


def _pdf_extract(data: bytes) -> tuple[str, str, str | None]:
    """Returns (title, text, date) from PDF bytes."""
    import pymupdf  # PyMuPDF >= 1.24 ("import fitz" is the legacy name)

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        meta = doc.metadata or {}
        pages = [page.get_text("text", sort=True) for page in doc]
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    raw_date = meta.get("creationDate") or ""  # e.g. "D:20240115093000Z"
    date = f"{raw_date[2:6]}-{raw_date[6:8]}-{raw_date[8:10]}" if raw_date.startswith("D:") and len(raw_date) >= 10 else None
    return (meta.get("title") or "").strip(), text, date


async def _render_with_browser(url: str) -> str | None:
    """JS fallback. Prefers Crawl4AI (gives clean markdown), else plain Playwright + Trafilatura."""
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

        async with AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False)) as crawler:
            result = await crawler.arun(
                url=url,
                config=CrawlerRunConfig(cache_mode=CacheMode.BYPASS, remove_overlay_elements=True,
                                        page_timeout=30000),
            )
            if result.success and result.markdown:
                return str(result.markdown.raw_markdown)
            return None
    except ImportError:
        pass

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            html = await page.content()
        finally:
            await browser.close()
    data = await asyncio.to_thread(_trafilatura_extract, html, url)
    return data.get("text") if data else None


class Extractor:
    def __init__(
        self,
        cache: Cache,
        *,
        user_agent: str,
        per_host_delay: float = 1.5,
        concurrency: int = 6,
        page_ttl: int = 7 * 24 * 3600,
        browser_fallback: bool = True,
    ) -> None:
        self._http = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(20.0, connect=8.0),
            headers={"User-Agent": user_agent, "Accept-Language": "en;q=0.9,*;q=0.5"},
            limits=httpx.Limits(max_connections=concurrency * 2),
            event_hooks={"request": [_guard_request]},
        )
        self._cache = cache
        self._throttle = HostThrottle(per_host_delay)
        self._robots = Robots(self._http, user_agent)
        self._sem = asyncio.Semaphore(concurrency)
        self._browser_sem = asyncio.Semaphore(2)  # headless browsers are heavy
        self._page_ttl = page_ttl
        self._browser_fallback = browser_fallback

    async def close(self) -> None:
        await self._http.aclose()

    async def fetch(self, hit: SearchHit) -> Document | None:
        url = hit.canonical_url
        key = Cache.key("page", url)
        cached = await self._cache.get_json(key)
        if cached:
            return self._to_doc(hit, **cached)
        if await self._cache.recently_failed(url):
            return None
        async with self._sem:
            try:
                result = await self._fetch_uncached(hit.url)
            except Exception as exc:  # network errors, parser crashes, browser timeouts
                log.info("fetch failed %s: %s", hit.url, exc)
                result = None
        if not result or len(result["text"]) < 200:
            await self._cache.mark_failed(url)
            return None
        await self._cache.set_json(key, result, self._page_ttl)
        return self._to_doc(hit, **result)

    async def _fetch_uncached(self, url: str) -> dict[str, Any] | None:
        if not await self._robots.allowed(url):
            log.info("robots.txt disallows %s", url)
            return None
        await self._throttle.wait(hostname(url))

        async with self._http.stream("GET", url) as resp:
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "").lower()
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    log.info("too large, skipped: %s", url)
                    return None
            final_url = str(resp.url)
            encoding = resp.encoding or "utf-8"

        if "application/pdf" in ctype or final_url.lower().endswith(".pdf") or body[:5] == b"%PDF-":
            title, text, date = await asyncio.to_thread(_pdf_extract, bytes(body))
            return {"title": title, "text": text, "date": date, "doc_type": "pdf"}

        html = body.decode(encoding, errors="replace")
        data = await asyncio.to_thread(_trafilatura_extract, html, final_url)
        text = (data or {}).get("text") or ""
        if len(text) >= MIN_TEXT_CHARS:
            return {"title": data.get("title") or "", "text": text, "date": data.get("date"), "doc_type": "html"}

        if self._browser_fallback and await is_public_host(hostname(final_url)):
            async with self._browser_sem:
                rendered = await _render_with_browser(final_url)
            if rendered and len(rendered) > len(text):
                return {"title": (data or {}).get("title") or "", "text": rendered,
                        "date": (data or {}).get("date"), "doc_type": "js-rendered"}
        return {"title": (data or {}).get("title") or "", "text": text, "date": (data or {}).get("date"),
                "doc_type": "html"} if text else None

    @staticmethod
    def _to_doc(hit: SearchHit, *, title: str, text: str, date: str | None, doc_type: str) -> Document:
        return Document(
            url=hit.url,
            canonical_url=hit.canonical_url,
            title=title or hit.title,
            text=text,
            date=date or hit.published,
            source=hostname(hit.url),
            engine=hit.engine,
            engines=hit.engines,
            query=hit.query,
            doc_type=doc_type,  # type: ignore[arg-type]
            content_hash=content_hash(text),
        )
