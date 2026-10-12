from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

Category = Literal["general", "news", "science", "it"]


@dataclass
class SearchQuery:
    q: str
    categories: Category = "general"
    time_range: Literal["day", "week", "month", "year"] | None = None
    engines: list[str] | None = None  # explicit engine names, e.g. ["arxiv", "pubmed"]


@dataclass
class SearchHit:
    url: str
    canonical_url: str
    title: str
    snippet: str
    engine: str
    engines: list[str]
    score: float
    category: str
    published: str | None
    query: str


@dataclass
class SearchResponse:
    query: SearchQuery
    hits: list[SearchHit]
    suggestions: list[str]
    unresponsive_engines: list[tuple[str, str]]


@dataclass
class Document:
    url: str
    canonical_url: str
    title: str
    text: str
    date: str | None
    source: str  # hostname, e.g. "arxiv.org"
    engine: str  # first SearXNG engine that returned it
    engines: list[str]
    query: str
    doc_type: Literal["html", "pdf", "js-rendered"]
    content_hash: str
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


@dataclass
class Chunk:
    doc: Document
    index: int
    text: str


@dataclass
class Passage:
    """A chunk pulled back from Qdrant, plus its rerank score."""

    text: str
    url: str
    title: str
    date: str | None
    source: str
    engine: str
    score: float = 0.0
    kind: Literal["fulltext", "snippet"] = "fulltext"  # snippet = search-result excerpt, page text not extracted


@dataclass
class SourceRecord:
    """Provenance of one retrieved source, kept for the whole run."""

    url: str
    title: str
    source: str
    engines: list[str]
    published: str | None
    extraction: Literal["extracted", "failed", "snippet_only", "duplicate_text", "not_fetched"]
    snippet: str = ""
    queries: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)


@dataclass
class Citation:
    number: int
    url: str
    title: str
    source: str
    date: str | None


@dataclass
class ResearchReport:
    question: str
    answer_markdown: str
    citations: list[Citation]
    sub_questions: list[str]
    rounds: int
    queries_run: list[str]
    open_gaps: list[str]
    plan: list[dict] = field(default_factory=list)       # sub-question, category, time range, queries
    sources: list[SourceRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)    # failed engines/searches/fetches, planner fallbacks
    coverage: dict[str, bool] = field(default_factory=dict)  # sub-question -> enough full-text evidence
    plan_source: str = ""
