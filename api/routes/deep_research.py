"""Thin proxy to research-stack's asynchronous job API.

The browser only talks to this API (same origin, via the Vite proxy); research-stack stays on localhost.
Nothing is stored here: jobs live in RabbitMQ/Celery/Valkey on the research-stack side.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
router = APIRouter(tags=["deep-research"])
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
TIMEOUT = httpx.Timeout(10.0)


class DeepResearchCreate(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    fresh: bool = False


async def _forward(request: Request, method: str, path: str, **kwargs: Any) -> Any:
    base = request.app.state.settings.research_stack_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.request(method, f"{base}{path}", **kwargs)
    except httpx.HTTPError as exc:
        log.warning("research-stack unreachable: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Research stack is unreachable. Is it running?") from exc
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", "Research stack error")
        except ValueError:
            detail = "Research stack error"
        raise HTTPException(status_code=resp.status_code, detail=detail if isinstance(detail, str) else "Invalid request")
    return resp.json()


@router.post("/deep-research", status_code=202)
async def submit_deep_research(body: DeepResearchCreate, request: Request) -> Any:
    return await _forward(request, "POST", "/research/jobs", json=body.model_dump())


@router.get("/deep-research")
async def list_deep_research(request: Request) -> Any:
    """History: recent jobs (question, status, created_at) from the job store."""
    return await _forward(request, "GET", "/research/jobs")


@router.get("/deep-research/{job_id}")
async def get_deep_research(job_id: str, request: Request) -> Any:
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="Unknown job id")
    return await _forward(request, "GET", f"/research/jobs/{job_id}")
