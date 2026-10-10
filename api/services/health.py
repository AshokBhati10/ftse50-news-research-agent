"""Dependency health checks — cheap, cached, and secret-free."""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

_CACHE_TTL_S = 60.0
_cache: dict[str, tuple[float, dict]] = {}


def _cached(key: str, fn):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]
    value = fn()
    _cache[key] = (now, value)
    return value


def check_searxng(settings) -> dict:
    """Lightweight reachability probe (GET / with a short timeout)."""
    def _probe() -> dict:
        base = (settings.searxng_base_url or "").rstrip("/")
        if not base:
            return {"ok": False, "detail": "SEARXNG_BASE_URL is not configured."}
        try:
            resp = httpx.get(base + "/", timeout=3.0, follow_redirects=True)
            ok = resp.status_code < 500
            return {"ok": ok,
                    "detail": f"HTTP {resp.status_code}" if ok else f"HTTP {resp.status_code} from SearXNG"}
        except Exception as exc:  # noqa: BLE001 — any failure means "unreachable"
            return {"ok": False, "detail": f"Unreachable: {type(exc).__name__}"}
    return _cached("searxng", _probe)


def check_ollama(settings) -> dict:
    """Config presence only — never the key, no network call per request."""
    if not settings.ollama_api_key:
        return {"ok": False, "detail": "OLLAMA_API_KEY is not set."}
    host = urlparse(settings.ollama_base_url or "").netloc or "not configured"
    return {"ok": True, "detail": f"Configured (model {settings.ollama_model}, host {host})."}


def check_you(settings) -> dict:
    if getattr(settings, "you_api_key", None):
        return {"ok": True, "detail": "You.com fallback API key is configured."}
    return {"ok": False, "detail": "YOU_API_KEY not set — SearXNG-only search."}


def cache_stats(settings) -> dict:
    from utils.cache import CacheManager
    try:
        cm = CacheManager(settings.cache_dir, enabled=True)
        return {"enabled": True, "entries": cm.size(), "dir": str(settings.cache_dir)}
    except Exception as exc:  # noqa: BLE001
        return {"enabled": True, "entries": 0, "error": str(exc)}
