from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import jobs
from .cache import Cache
from .config import settings
from .searx_client import SearxClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)
cache: Cache | None = None
searx: SearxClient | None = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # The API only enqueues and reads jobs; the ML models live in the Celery worker, so this process
    # starts fast and stays small.
    global cache, searx
    cache = Cache(settings.valkey_url)
    searx = SearxClient(settings.searxng_url, cache)
    yield
    await searx.close()
    await cache.close()


app = FastAPI(title="Yantra3loka Research Agent", lifespan=lifespan)


class ResearchRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    fresh: bool = False  # bypass the 30-minute identical-question de-duplication


def _unavailable(exc: Exception) -> HTTPException:
    log.error("job backend unavailable: %s", type(exc).__name__)
    return HTTPException(status_code=503, detail="job queue unavailable (RabbitMQ/Valkey); try again shortly")


@app.post("/research/jobs", status_code=202)
async def create_job(req: ResearchRequest) -> dict[str, Any]:
    """Queue a research run and return its job id right away."""
    try:
        return await jobs.submit_job(req.question.strip(), fresh=req.fresh)
    except Exception as exc:
        raise _unavailable(exc) from exc


@app.get("/research/jobs")
async def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    """Recent jobs (newest first) with question and status; fetch one by id for its report."""
    try:
        return await jobs.list_jobs(max(1, min(limit, 100)))
    except Exception as exc:
        raise _unavailable(exc) from exc


@app.get("/research/jobs/{job_id}")
async def job_status(job_id: str) -> dict[str, Any]:
    """Status (pending/running/completed/failed); the report is included once completed."""
    try:
        doc = await jobs.get_job(job_id)
    except Exception as exc:
        raise _unavailable(exc) from exc
    if doc["status"] == "unknown":
        raise HTTPException(status_code=404, detail="unknown job id")
    return doc


@app.get("/research/jobs/{job_id}/result")
async def job_result(job_id: str) -> dict[str, Any]:
    """The finished report only: 200 when completed, 409 while pending/running, 500 detail when failed."""
    doc = await job_status(job_id)
    if doc["status"] == "completed":
        return doc["result"]
    if doc["status"] == "failed":
        raise HTTPException(status_code=500, detail=doc.get("error", "failed"))
    raise HTTPException(status_code=409, detail=f"job is {doc['status']}")


@app.post("/research")
async def research(req: ResearchRequest) -> Any:
    """Backwards-compatible blocking endpoint: runs the job on a worker and waits for the report.

    If the run takes longer than SYNC_WAIT_SECONDS the job keeps running and a 202 with the job id is returned;
    use GET /research/jobs/{id} to collect it.
    """
    try:
        job = await jobs.submit_job(req.question.strip(), fresh=req.fresh)
    except Exception as exc:
        raise _unavailable(exc) from exc
    deadline = asyncio.get_running_loop().time() + settings.sync_wait_seconds
    while True:
        doc = await jobs.get_job(job["job_id"])
        if doc["status"] == "completed":
            return doc["result"]
        if doc["status"] == "failed":
            raise HTTPException(status_code=500, detail=doc.get("error", "failed"))
        if asyncio.get_running_loop().time() >= deadline:
            return JSONResponse(status_code=202, content={"job_id": job["job_id"], "status": doc["status"],
                                                          "detail": "still running; poll GET /research/jobs/{job_id}"})
        await asyncio.sleep(2)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    assert searx is not None and cache is not None
    broker, workers = await asyncio.gather(
        asyncio.to_thread(jobs.broker_ok_sync), asyncio.to_thread(jobs.workers_online_sync))
    return {"searxng": await searx.healthy(), "valkey": await cache.ping(), "rabbitmq": broker,
            "workers": workers}


@app.get("/engines/errors")
async def engine_errors() -> dict[str, Any]:
    """Proxy of SearXNG /stats/errors so you can watch upstream health without exposing SearXNG."""
    assert searx is not None
    return await searx.engine_errors()
