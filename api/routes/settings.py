"""Operational configuration status.

Reports *whether* the LLM/search services are configured and which model
and hosts are in use. Secrets (API keys, tokens) are never returned.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Request

from api.schemas import SettingsStatus

router = APIRouter(tags=["settings"])


def _host(url: str | None) -> str:
    if not url:
        return "not configured"
    return urlparse(url).netloc or "not configured"


@router.get("/settings/status", response_model=SettingsStatus)
def get_settings_status(request: Request) -> SettingsStatus:
    settings = request.app.state.settings
    warnings: list[str] = []
    if not settings.ollama_api_key:
        warnings.append("OLLAMA_API_KEY is not set — live research runs and LLM analysis are disabled.")
    if not settings.searxng_base_url:
        warnings.append("SEARXNG_BASE_URL is not set — news search cannot run.")
    if not getattr(settings, "you_api_key", None):
        warnings.append("YOU_API_KEY is not set — no fallback search provider available.")

    return SettingsStatus(
        ollama_configured=bool(settings.ollama_api_key),
        ollama_model=settings.ollama_model,
        ollama_host=_host(settings.ollama_base_url),
        searxng_base_url=settings.searxng_base_url or "",
        searxng_configured=bool(settings.searxng_base_url),
        you_fallback_configured=bool(getattr(settings, "you_api_key", None)),
        defaults={
            "top_n_articles": settings.top_n_articles,
            "max_articles_per_company": settings.max_articles_per_company,
            "max_search_concurrency": settings.max_search_concurrency,
            "max_llm_concurrency": settings.max_llm_concurrency,
            "lookback_days": settings.news_lookback_days,
            "dedup_threshold": settings.dedup_similarity_threshold,
            "cache_enabled": True,  # per-run `no_cache` can disable it
        },
        warnings=warnings,
    )
