from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from api.schemas import ResearchRunCreate, ResearchRunOut, RunsResponse
from api.services.runs import (
    InvalidRunRequestError,
    RunConflictError,
    RunNotFoundError,
)

log = logging.getLogger(__name__)
router = APIRouter(tags=["research-runs"])


@router.post("/research-runs", response_model=ResearchRunOut, status_code=201)
async def create_research_run(body: ResearchRunCreate, request: Request) -> ResearchRunOut:
    manager = request.app.state.run_manager
    settings = request.app.state.settings
    try:
        state = await manager.create_run(body, settings)
    except RunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidRunRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return state.to_out()


@router.get("/research-runs", response_model=RunsResponse)
def list_research_runs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> RunsResponse:
    manager = request.app.state.run_manager
    runs = manager.list_runs(limit=limit)
    return RunsResponse(count=len(runs), runs=runs)


@router.get("/research-runs/{run_id}", response_model=ResearchRunOut)
async def get_research_run(run_id: str, request: Request) -> ResearchRunOut:
    manager = request.app.state.run_manager
    try:
        state = await manager.get(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown run: {run_id!r}") from exc
    return state.to_out()


@router.post("/research-runs/{run_id}/cancel", response_model=ResearchRunOut)
async def cancel_research_run(run_id: str, request: Request) -> ResearchRunOut:
    manager = request.app.state.run_manager
    try:
        state = await manager.cancel(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown run: {run_id!r}") from exc
    return state.to_out()


@router.get("/research-runs/{run_id}/events")
async def research_run_events(run_id: str, request: Request) -> StreamingResponse:
    manager = request.app.state.run_manager
    try:
        await manager.get(run_id)  # 404 early if unknown
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown run: {run_id!r}") from exc

    async def _gen():
        async for event in manager.events(run_id):
            yield f"data: {json.dumps(event, default=str)}\n\n"
        # Terminal event consumed — close the stream cleanly.

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
