"""Typed request/response models for the FTSE 50 News Research Agent API.

These schemas are the API contract consumed by the React frontend. They are
deliberately *derived from* (never duplicating) the pipeline models in
``utils/models.py``. No secrets may appear in any response schema.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


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
    title: str = ""                  # user query for query reports, else id
    report_type: str = "ftse"        # "ftse" | "query"
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


class QueryFindingOut(BaseModel):
    title: str
    url: str
    source: str
    published_at: datetime | None = None
    relevance: float = 0.0
    key_finding: str = ""
    significance: str = ""
    impact: str
    sentiment: str
    reason: str = ""
    confidence: float = 0.0
    source_quality: float | None = None
    evidence_excerpt: str = ""


class SourceRecordOut(BaseModel):
    title: str
    url: str
    source_name: str = ""
    published_at: datetime | None = None
    supporting_excerpt: str = ""
    supports_claims: list[int] = Field(default_factory=list)


class QueryAnswerOut(BaseModel):
    query: str
    task_type: str
    answer: str
    key_points: list[str] = Field(default_factory=list)
    sources: list[SourceRecordOut] = Field(default_factory=list)
    confidence: float = 0.0
    limitations: list[str] = Field(default_factory=list)
    verified: bool = True


class ReportDetail(BaseModel):
    summary: ReportSummary
    companies: list[ReportCompany] = Field(default_factory=list)  # legacy FTSE
    findings: list[QueryFindingOut] = Field(default_factory=list)  # query mode
    answer: QueryAnswerOut | None = None  # agentic answer-first (query mode)
    query: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
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
    # Query-driven research (primary). Mutually exclusive with tickers.
    query: str | None = Field(
        default=None,
        description="Arbitrary research question or topic, e.g. "
                    "'Latest developments in semiconductor stocks'.",
        max_length=500,
    )
    max_results: int | None = Field(
        default=None, ge=1, le=50,
        description="Max articles to analyse for a query run (default 20).",
    )
    time_range: str | None = Field(
        default=None,
        description="Recency bias for query search: day | week | month | year.",
        pattern="^(day|week|month|year)$",
    )
    fresh_search: bool = Field(
        default=False,
        description="Bypass cached search results for this query run.",
    )
    # Legacy FTSE company research (secondary).
    tickers: list[str] | None = Field(
        default=None,
        description="Subset of FTSE 50 tickers to research (e.g. ['AZN.L']). "
                    "Omit or empty for the full configured universe.",
        max_length=60,
    )
    limit_companies: int | None = Field(default=None, ge=1, le=60)
    agentic: bool | None = Field(
        default=None,
        description="Query runs: use the agentic research loop (task planning + "
                    "bounded tool calling + direct answer). Defaults to True for "
                    "query runs. Company runs: let the Pydantic AI agent drive "
                    "per-company research (defaults to False).",
    )
    no_cache: bool = False

    @field_validator("query")
    @classmethod
    def _strip_query(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("Query must not be empty or whitespace-only.")
        return v

    @model_validator(mode="after")
    def _mutually_exclusive(self) -> "ResearchRunCreate":
        if self.query and self.tickers:
            raise ValueError("Provide either 'query' or 'tickers', not both.")
        return self

    def resolved_agentic(self) -> bool:
        """Agentic loop defaults ON for query runs, OFF for company runs."""
        if self.agentic is not None:
            return self.agentic
        return self.query is not None


class CompanyProgress(BaseModel):
    ticker: str
    status: str                      # completed | failed
    articles: int = 0
    notes: str = ""


class ResearchRunOut(BaseModel):
    run_id: str
    status: RunStatus
    mode: str = "companies"              # "companies" | "query"
    query: str | None = None             # original user query (query mode)
    stage: str | None = None             # current pipeline stage (query mode)
    stats: dict[str, Any] = Field(default_factory=dict)  # counts etc. (query mode)
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
