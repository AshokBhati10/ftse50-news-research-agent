"""Phase 3 — answer presentation: structured findings, dates, links, limits."""

from __future__ import annotations

from datetime import datetime, timezone

from agent.research_agent import AgentResult
from config.settings import Settings
from nodes.output import build_answer_report, render_answer_markdown
from utils.models import (
    QueryAnswer,
    ResearchPlan,
    SourceRecord,
    TaskType,
)


def _report_with_citations():
    answer = QueryAnswer(
        query="AstraZeneca latest developments",
        task_type=TaskType.NEWS_RESEARCH,
        answer="AstraZeneca announced a development.",
        key_points=["AstraZeneca announced a $2bn development."],
        sources=[SourceRecord(
            title="AstraZeneca in $2bn deal",
            url="https://reuters.com/x",
            source_name="Reuters",
            published_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
            supporting_excerpt="AstraZeneca announced the deal.",
            supports_claims=[0],
        )],
        confidence=0.8, verified=True, limitations=[],
    )
    plan = ResearchPlan(task_type=TaskType.NEWS_RESEARCH, intent_summary="x")
    evidence = [{
        "title": "AstraZeneca in $2bn deal", "url": "https://reuters.com/x",
        "source": "reuters.com", "published_at": "2026-10-05T00:00:00+00:00",
        "text": "AstraZeneca announced the deal.", "kind": "search",
    }]
    result = AgentResult(
        query=answer.query, plan=plan, answer=answer, findings=[],
        counts={"max_results": 2, "tool_calls": 1, "candidates_considered": 5,
                "evidence_items": 1, "sources_cited": 1},
        warnings=[], tool_trace=[], evidence=evidence)
    return build_answer_report(
        query=answer.query, run_id="run_x", started_at=datetime.now(timezone.utc),
        result=result, settings=Settings())


def test_key_points_carry_inline_links_and_dates():
    md = render_answer_markdown(_report_with_citations())
    # Key point line ends with a clickable source and its date.
    assert "[Reuters](https://reuters.com/x), 2026-10-05" in md
    # The same source is also listed in the Sources section.
    assert "## Sources" in md


def test_confidence_and_limitations_always_rendered():
    md = render_answer_markdown(_report_with_citations())
    assert "## Confidence & limitations" in md
    assert "No specific limitations recorded." in md  # transparent fallback


def test_evidence_base_reports_distinct_and_dated_counts():
    report = _report_with_citations()
    assert report.counts["dated_evidence_items"] == 1
    md = render_answer_markdown(report)
    assert ("Evidence base: 1 distinct article(s) (1 dated) · 1 cited source(s)"
            in md)


def test_unverified_answer_surfaces_limitations():
    report = _report_with_citations()
    report.answer.verified = False
    report.answer.confidence = 0.0
    report.answer.limitations = ["No dated evidence was retrieved."]
    md = render_answer_markdown(report)
    assert "(unverified)" in md
    assert "No dated evidence was retrieved." in md


def test_undated_source_shows_undated_label():
    report = _report_with_citations()
    report.answer.sources[0].published_at = None
    md = render_answer_markdown(report)
    assert "[Reuters](https://reuters.com/x), undated" in md
