from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request

from api.schemas import CompaniesResponse, CompanyOut
from utils.cache import CacheManager

log = logging.getLogger(__name__)
router = APIRouter(tags=["companies"])


@router.get("/companies", response_model=CompaniesResponse)
async def list_companies(
    request: Request,
    refresh: bool = Query(default=False, description="Bypass the cached FTSE 50 list"),
) -> CompaniesResponse:
    from nodes.ingestion import build_ftse50

    settings = request.app.state.settings
    cache = CacheManager(settings.cache_dir, enabled=not refresh)
    try:
        companies = await build_ftse50(settings, cache)
    except Exception as exc:  # noqa: BLE001 — surface honestly, never a traceback
        log.exception("Company ingestion failed")
        raise HTTPException(status_code=502,
                            detail=f"Could not load the FTSE 50 company list: {exc}") from exc
    # Warm the run manager's ticker-validation cache.
    request.app.state.run_manager.set_company_cache(companies)
    return CompaniesResponse(
        count=len(companies),
        companies=[CompanyOut(name=c.name, ticker=c.ticker, market_cap=c.market_cap)
                   for c in companies],
    )
