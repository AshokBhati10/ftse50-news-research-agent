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


# ---------------------------------------------------------------------------
# Agentic query research: plans, tool calls, answers
# ---------------------------------------------------------------------------

class TaskType(str, Enum):
    FACTUAL_LOOKUP = "factual_lookup"    # who/what/when — one verified answer
    NEWS_RESEARCH = "news_research"      # recent developments, why did X happen
    COMPARISON = "comparison"            # compare A vs B on explicit criteria
    SUMMARIZATION = "summarization"      # summarize a specific article/URL
    GENERAL = "general"                  # anything else


PERMITTED_TOOLS = ("web_search", "fetch_article")


class ResearchPlan(BaseModel):
    """Task-specific plan produced by the planner (LLM or heuristic fallback)."""

    task_type: TaskType
    intent_summary: str = Field(max_length=280)
    needs_web_research: bool = True
    tools_allowed: list[str] = Field(default_factory=lambda: ["web_search", "fetch_article"])
    search_queries: list[str] = Field(default_factory=list)
    evidence_needed: str = Field(default="", max_length=500)
    expected_answer_format: str = Field(default="", max_length=280)
    completion_criteria: str = Field(default="", max_length=500)
    detailed: bool = False  # True -> also produce ranked findings
    max_tool_calls: int = Field(default=5, ge=1, le=10)

    @field_validator("tools_allowed")
    @classmethod
    def _only_permitted(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if t not in PERMITTED_TOOLS]
        if bad:
            raise ValueError(f"Unsupported tools in plan: {bad}")
        return v

    @field_validator("search_queries")
    @classmethod
    def _bounded_queries(cls, v: list[str]) -> list[str]:
        if len(v) > 5:
            raise ValueError("Plan must not request more than 5 search queries")
        return [q.strip() for q in v if q.strip()]


class AgentStep(BaseModel):
    """One decision in the controlled tool-calling loop."""

    action: Literal["call_tool", "finish"]
    tool_name: str = ""
    arguments: dict = Field(default_factory=dict)
    reason: str = Field(default="", max_length=280)

    @field_validator("tool_name")
    @classmethod
    def _known_tool(cls, v: str, info) -> str:
        action = (info.data or {}).get("action")
        if action == "call_tool" and v not in PERMITTED_TOOLS:
            raise ValueError(f"Unknown tool: {v!r}")
        return v


class SourceRecord(BaseModel):
    """A citation supporting the answer's claims."""

    title: str
    url: str
    source_name: str = ""
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    supporting_excerpt: str = Field(default="", max_length=600)
    supports_claims: list[int] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def _url_must_be_http(cls, v: str) -> str:
        v = v.strip()
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError(f"source url must be absolute http(s): {v!r}")
        return v

    @field_validator("supports_claims")
    @classmethod
    def _non_negative(cls, v: list[int]) -> list[int]:
        if any(i < 0 for i in v):
            raise ValueError("supports_claims indices must be >= 0")
        return v


class ToolTraceEntry(BaseModel):
    tool: str
    arguments_summary: str = ""
    observation_summary: str = ""
    ok: bool = True


class QueryAnswer(BaseModel):
    """The direct, evidence-backed answer to the user's question."""

    query: str
    task_type: TaskType = TaskType.GENERAL
    answer: str = Field(min_length=1, description="Direct answer, not a result list.")
    key_points: list[str] = Field(default_factory=list)
    sources: list[SourceRecord] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    limitations: list[str] = Field(default_factory=list)
    tool_trace: list[ToolTraceEntry] = Field(default_factory=list)
    verified: bool = True

    @field_validator("answer")
    @classmethod
    def _answer_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("answer must not be blank")
        return v.strip()

    @classmethod
    def unverified(cls, query: str, task_type: TaskType,
                   limitations: list[str]) -> "QueryAnswer":
        """Honest fallback when the evidence cannot support an answer."""
        return cls(
            query=query,
            task_type=task_type,
            answer=("I could not verify an answer to this question from the "
                    "sources retrieved."),
            key_points=[],
            sources=[],
            confidence=0.0,
            limitations=limitations or ["Insufficient evidence retrieved."],
            verified=False,
        )


# ---------------------------------------------------------------------------
# Query-driven research models
# ---------------------------------------------------------------------------

class QueryArticleScore(BaseModel):
    """Per-article judgement for query-driven research (one batched LLM call)."""

    index: int = Field(ge=0, description="Index of the article in the submitted batch.")
    relevance: float = Field(ge=0.0, le=1.0,
                             description="How directly the article answers the user's query.")
    key_finding: str = Field(max_length=280, description="The article's main finding, one sentence.")
    significance: str = Field(max_length=280,
                              description="Why it matters (business/financial/technical), one sentence.")
    impact: ImpactLevel = Field(description="Importance of the development; Low when not applicable.")
    sentiment: Sentiment = Field(description="Only where meaningful; otherwise Neutral.")
    reason: str = Field(max_length=280, description="One concise sentence justifying impact.")
    confidence: float = Field(ge=0.0, le=1.0)


class QueryAnalysisResult(BaseModel):
    """Structured LLM output: a score for every candidate article submitted."""

    scores: list[QueryArticleScore]

    @field_validator("scores")
    @classmethod
    def _non_empty(cls, v: list[QueryArticleScore]) -> list[QueryArticleScore]:
        if not v:
            raise ValueError("analysis must score at least one article")
        return v


class QueryFinding(BaseModel):
    """A ranked finding from query-driven research, with evidence tracking."""

    title: str
    url: str
    source: str
    published_at: datetime | None
    relevance: float = Field(ge=0.0, le=1.0)
    key_finding: str = ""
    significance: str = ""
    impact: ImpactLevel
    sentiment: Sentiment
    reason: str = ""
    confidence: float = 0.0
    source_quality: float = 0.0
    evidence_excerpt: str = ""


class QueryReport(BaseModel):
    """Machine-readable report for a query-driven research run."""

    report_type: Literal["query"] = "query"
    query: str
    run_id: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    search_config: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    counts: dict = Field(default_factory=dict)
    findings: list[QueryFinding] = Field(default_factory=list)
    config_snapshot: dict = Field(default_factory=dict)
    # Agentic answer-first fields (populated by ResearchAgent runs).
    answer: QueryAnswer | None = None
    plan_summary: dict = Field(default_factory=dict)
