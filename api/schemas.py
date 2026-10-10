"""Typed request/response models for the FTSE 50 News Research Agent API.

These schemas are the API contract consumed by the React frontend. They are
deliberately *derived from* (never duplicating) the pipeline models in
``utils/models.py``. No secrets may appear in any response schema.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class DependencyStatus(BaseModel):
    ok: bool
    detail: str = ""


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
    searxng: DependencyStatus
    ollama: DependencyStatus          # config presence only — never the key
    you_fallback: DependencyStatus
    cache: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------

class CompanyOut(BaseModel):
    name: str
    ticker: str
    market_cap: float | None = None


class CompaniesResponse(BaseModel):
    count: int
    companies: list[CompanyOut]


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

class ReportSummary(BaseModel):
    id: str                          # e.g. "report_2026-10-10"
    date: str                        # YYYY-MM-DD
    generated_at: datetime | None = None
    company_count: int = 0
    article_count: int = 0
    companies: list[str] = Field(default_factory=list)  # tickers


class ReportArticle(BaseModel):
    title: str
    url: str
    source: str
    published_at: datetime | None = None
    impact: str
    sentiment: str
    confidence: float
    reason: str = ""
    evidence_excerpt: str = ""
    source_quality: float | None = None
    source_quality_tier: str | None = None


class ReportCompany(BaseModel):
    company: str
    ticker: str
    market_cap: float | None = None
    candidates_considered: int = 0
    providers_used: list[str] = Field(default_factory=list)
    fallback_used: bool = False
    notes: str = ""
    articles: list[ReportArticle] = Field(default_factory=list)


class ReportDetail(BaseModel):
    summary: ReportSummary
    companies: list[ReportCompany]
    has_markdown: bool = False


class ReportsResponse(BaseModel):
    count: int
    reports: list[ReportSummary]


# ---------------------------------------------------------------------------
# Research runs
# ---------------------------------------------------------------------------

class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class ResearchRunCreate(BaseModel):
    tickers: list[str] | None = Field(
        default=None,
        description="Subset of FTSE 50 tickers to research (e.g. ['AZN.L']). "
                    "Omit or empty for the full configured universe.",
        max_length=60,
    )
    limit_companies: int | None = Field(default=None, ge=1, le=60)
    agentic: bool = False
    no_cache: bool = False


class CompanyProgress(BaseModel):
    ticker: str
    status: str                      # completed | failed
    articles: int = 0
    notes: str = ""


class ResearchRunOut(BaseModel):
    run_id: str
    status: RunStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    total_companies: int = 0
    completed: int = 0
    failed: int = 0
    current_company: str | None = None
    articles_found: int = 0
    error: str | None = None
    report_id: str | None = None
    companies: list[CompanyProgress] = Field(default_factory=list)
    request: dict[str, Any] = Field(default_factory=dict)


class RunsResponse(BaseModel):
    count: int
    runs: list[ResearchRunOut]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class SettingsStatus(BaseModel):
    ollama_configured: bool
    ollama_model: str
    ollama_host: str                 # host only — never credentials
    searxng_base_url: str
    searxng_configured: bool
    you_fallback_configured: bool
    defaults: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)
