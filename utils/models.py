"""Shared Pydantic models — the single source of truth for pipeline data."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ImpactLevel(str, Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class Sentiment(str, Enum):
    POSITIVE = "Positive"
    NEGATIVE = "Negative"
    NEUTRAL = "Neutral"


class Company(BaseModel):
    """A listed company selected for news research."""

    name: str
    ticker: str  # e.g. "AZN.L"
    market_cap: float | None = None
    currency: str = "GBP"


class Article(BaseModel):
    """Normalized internal article representation used across the pipeline."""

    company: str
    ticker: str
    title: str
    url: str
    source: str = ""
    published_at: datetime | None = None
    snippet: str = ""
    content: str | None = None
    search_provider: str = ""  # searxng | you.com | yfinance | mock
    extraction_status: Literal["pending", "ok", "snippet_only", "failed", "mock"] = "pending"
    source_quality: float = 0.0
    source_quality_tier: str = ""
    evidence_excerpt: str = ""

    @field_validator("url")
    @classmethod
    def _url_must_be_http(cls, v: str) -> str:
        v = v.strip()
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError(f"article url must be absolute http(s): {v!r}")
        return v

    def text_for_analysis(self, max_chars: int = 1200) -> str:
        """Best available text body for the LLM: extracted content, else snippet."""
        body = (self.content or "").strip() or self.snippet.strip()
        return body[:max_chars]


class ArticleScore(BaseModel):
    """Per-article judgement produced by the LLM (one batched call per company)."""

    index: int = Field(ge=0, description="Index of the article in the submitted batch.")
    impact: ImpactLevel
    sentiment: Sentiment
    reason: str = Field(max_length=280, description="One concise sentence.")
    confidence: float = Field(ge=0.0, le=1.0)


class AnalysisResult(BaseModel):
    """Structured LLM output: a score for every candidate article submitted."""

    scores: list[ArticleScore]

    @field_validator("scores")
    @classmethod
    def _non_empty(cls, v: list[ArticleScore]) -> list[ArticleScore]:
        if not v:
            raise ValueError("analysis must score at least one article")
        return v


class RankedArticle(BaseModel):
    """A final top-N finding with full evidence/citation tracking."""

    title: str
    url: str
    source: str
    published_at: datetime | None
    impact: ImpactLevel
    sentiment: Sentiment
    reason: str
    confidence: float
    source_quality: float
    evidence_excerpt: str = ""


class CompanyReport(BaseModel):
    company: str
    ticker: str
    market_cap: float | None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    articles: list[RankedArticle] = Field(default_factory=list)
    candidates_considered: int = 0
    providers_used: list[str] = Field(default_factory=list)
    fallback_used: bool = False
    notes: str = ""


class FullReport(BaseModel):
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    company_count: int = 0
    companies: list[CompanyReport] = Field(default_factory=list)
    config_snapshot: dict = Field(default_factory=dict)


# Backwards/forwards compatibility alias used by older prompt sketches.
NewsAnalysis = AnalysisResult
