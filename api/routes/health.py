from __future__ import annotations

from fastapi import APIRouter, Request

from api.schemas import DependencyStatus, HealthResponse
from api.services import health as health_svc

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def get_health(request: Request) -> HealthResponse:
    settings = request.app.state.settings
    searxng = health_svc.check_searxng(settings)
    ollama = health_svc.check_ollama(settings)
    you = health_svc.check_you(settings)
    return HealthResponse(
        searxng=DependencyStatus(**searxng),
        ollama=DependencyStatus(**ollama),
        you_fallback=DependencyStatus(**you),
        cache=health_svc.cache_stats(settings),
    )
