"""Tests for nodes/output.py — JSON + Markdown report generation."""

from datetime import datetime, timezone

from config.settings import Settings
from nodes.output import build_full_report, render_markdown, write_reports
from utils.models import CompanyReport, ImpactLevel, RankedArticle, Sentiment


def _report() -> CompanyReport:
    return CompanyReport(
        company="AstraZeneca", ticker="AZN.L", market_cap=196_400_000_000,
        candidates_considered=10,
        providers_used=["searxng", "yfinance"],
        articles=[
            RankedArticle(
                title="AstraZeneca to acquire Fusion Therapeutics in $2.4bn deal",
                url="https://www.reuters.com/business/x",
                source="reuters.com",
                published_at=datetime(2026, 10, 7, 9, 15, tzinfo=timezone.utc),
                impact=ImpactLevel.HIGH, sentiment=Sentiment.POSITIVE,
                reason="Major acquisition, clearly market-moving.",
                confidence=0.92, source_quality=0.8,
                evidence_excerpt="AstraZeneca agreed to acquire Fusion Therapeutics…",
            ),
        ],
    )


def test_markdown_contains_citations_and_badges(tmp_path):
    settings = Settings()
    full = build_full_report([_report()], settings)
    md = render_markdown(full)
    assert "AstraZeneca (AZN.L)" in md
    assert "https://www.reuters.com/business/x" in md  # citation URL present
    assert "🔴 High" in md
    assert "🟢 Positive" in md
    assert "Why:" in md
    assert "£196.4B" in md


def test_markdown_handles_empty_company():
    settings = Settings()
    empty = CompanyReport(company="X", ticker="X.L", market_cap=None,
                          notes="No candidate articles found; skipping.")
    full = build_full_report([empty], settings)
    md = render_markdown(full)
    assert "No candidate articles found" in md


def test_write_reports_creates_both_files(tmp_path):
    import json

    settings = Settings()
    full = build_full_report([_report()], settings)
    json_path, md_path = write_reports(full, tmp_path)

    assert json_path.exists() and md_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["company_count"] == 1
    company = payload["companies"][0]
    assert company["ticker"] == "AZN.L"
    article = company["articles"][0]
    # Every finding keeps its evidence.
    for field in ("title", "url", "source", "published_at", "impact",
                  "sentiment", "reason", "confidence", "evidence_excerpt"):
        assert field in article, field
    assert article["impact"] == "High"
