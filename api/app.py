"""FastAPI application for the FTSE 50 News Research Agent.

Serves the React frontend's API: health, companies, reports, research runs
(SSE progress) and settings status. The research pipeline itself is reused
unchanged from ``nodes/`` — this layer only orchestrates and presents.

Run locally with::

    uv run uvicorn api.app:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import companies, deep_research, health, reports, runs, settings as settings_routes
from api.services.runs import RunManager
from config.settings import Settings

log = logging.getLogger("ftse50.api")


def _cors_origins() -> list[str]:
    raw = os.getenv("FRONTEND_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    return [o.strip() for o in raw.split(",") if o.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = Settings()  # reads .env once at startup
    runs_dir = app.state.settings.cache_dir / "runs"
    app.state.run_manager = RunManager(runs_dir)
    app.state.run_manager.recover()
    log.info("API ready; CORS origins=%s", _cors_origins())
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="FTSE 50 News Research Agent API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    for router in (health.router, companies.router, reports.router,
                   runs.router, settings_routes.router, deep_research.router):
        app.include_router(router, prefix="/api")
    return app


app = create_app()
