"""Regression tests for evidence quality and cross-section consistency.

Covers: market-cap validation (units/date/context), story de-duplication,
snippet vs full-article distinction, FTSE universe scoping, and the
verification step that stops an answer claiming more than its evidence can
support.
"""

from __future__ import annotations

import pytest

from config.settings import Settings
from utils.financial_validation import (
    assess_market_cap_evidence,
    answer_claims_ranking,
    is_market_cap_question,
    is_ranking_question,
    unmatched_figures,
)
from utils.query_scope import mentions_universe, requested_universe
from utils.story_dedup import dedup_stories, is_near_duplicate_title
from agent.evidence import (
    evidence_from_fetch,
    evidence_from_search,
    is_full_article,
    select_evidence,
)
from agent.research_agent import ResponseParser
from agent.verification import filter_findings_for_universe, verify_answer
from utils.models import (
    ImpactLevel,
    QueryAnswer,
    QueryFinding,
    Sentiment,
    TaskType,
)


# ---------------------------------------------------------------------------
# Market-cap evidence validation
# ---------------------------------------------------------------------------

FTSE_RANK_Q = ("Give top 15 FTSE companies of last week and rank according "
               "to their market cap")


def test_market_cap_question_detection():
    assert is_market_cap_question(FTSE_RANK_Q)
    assert is_ranking_question(FTSE_RANK_Q)
    assert not is_market_cap_question("Who is the CEO of HSBC?")
    assert answer_claims_ranking("Ranked by market cap: 1. X")
    assert not answer_claims_ranking("Revenue rose 5% last quarter.")


def test_bare_number_is_not_a_usable_market_cap():
    evidence = [{"title": "Yahoo Finance",
                 "url": "https://finance.yahoo.com/x",
                 "text": "AstraZeneca 238.677", "kind": "search"}]
    a = assess_market_cap_evidence(evidence, text_getter=lambda e: e["text"],
                                   query=FTSE_RANK_Q)
    assert a.requested and a.ranking_requested
    assert not a.usable  # no metric, currency/unit, or as-of date
    assert a.reason


def test_market_cap_with_metric_unit_and_date_is_usable():
    evidence = [
        {"url": "https://finance.yahoo.com/a",
         "text": "AstraZeneca market cap £150.0 billion as of 2026-10-01"},
        {"url": "https://www.londonstockexchange.com/b",
         "text": "HSBC Holdings market cap £120.5 billion as of 2026-10-01"},
    ]
    a = assess_market_cap_evidence(evidence, text_getter=lambda e: e["text"],
                                   query=FTSE_RANK_Q)
    assert a.usable
    assert len([v for v in a.values if v.usable]) >= 2


def test_mixed_currencies_are_not_comparable():
    evidence = [
        {"url": "https://finance.yahoo.com/a",
         "text": "AstraZeneca market cap £150.0 billion as of 2026-10-01"},
        {"url": "https://www.bloomberg.com/b",
         "text": "Apple Inc market cap $500.0 billion as of 2026-10-01"},
    ]
    a = assess_market_cap_evidence(evidence, text_getter=lambda e: e["text"],
                                   query=FTSE_RANK_Q)
    assert not a.usable
    assert "currenc" in a.reason.lower()


def _assess(evidence):
    return assess_market_cap_evidence(
        evidence, text_getter=lambda e: e["text"], query=FTSE_RANK_Q)


def test_market_cap_must_be_bound_to_a_company():
    # Two values, no company attached to either -> not a ranking.
    a = _assess([{"url": "https://finance.yahoo.com/a",
                  "text": "Market cap £150.0 billion and £120.5 billion "
                          "as of 2026-10-01"}])
    assert not a.usable


def test_ranking_rejects_low_tier_sources():
    ev = [
        {"url": "https://randomblog.example/a",
         "text": "AstraZeneca market cap £150.0 billion as of 2026-10-01"},
        {"url": "https://randomblog.example/b",
         "text": "HSBC Holdings market cap £120.5 billion as of 2026-10-01"},
    ]
    a = _assess(ev)
    assert not a.usable
    assert "source" in a.reason


def test_republished_copies_are_not_independent():
    text = "AstraZeneca market cap £150.0 billion as of 2026-10-01"
    a = _assess([{"url": "https://finance.yahoo.com/a", "text": text},
                 {"url": "https://www.bloomberg.com/b", "text": text}])
    assert not a.usable  # one company, one underlying statement


def test_unmatched_figures_flags_unsupported_numbers():
    missing = unmatched_figures(
        "AstraZeneca invested $2bn and £500m in the project.",
        ["AstraZeneca said it would invest in the project."],
    )
    assert any("2bn" in m for m in missing)
    present = unmatched_figures(
        "AstraZeneca invested $2bn.",
        ["AstraZeneca invested $2bn in the project."],
    )
    assert present == []


# ---------------------------------------------------------------------------
# Story de-duplication
# ---------------------------------------------------------------------------

def test_identical_syndicated_headlines_collapse():
    assert is_near_duplicate_title(
        "AstraZeneca to buy Summit Therapeutics in $2bn deal",
        "AstraZeneca to buy Summit Therapeutics in $2bn deal",
    )
    items = [
        {"title": "AstraZeneca to buy Summit Therapeutics in $2bn deal",
         "url": "https://reuters.com/a"},
        {"title": "AstraZeneca to buy Summit Therapeutics in $2bn deal",
         "url": "https://news.example/a"},
    ]
    out = dedup_stories(items, title_getter=lambda e: e["title"])
    assert len(out) == 1
    assert out[0]["url"] == "https://reuters.com/a"


def test_distinct_stories_sharing_a_company_are_kept():
    items = [
        {"title": "AstraZeneca invests in Summit Therapeutics"},
        {"title": "AstraZeneca to invest over $1bn in Massachusetts operations"},
    ]
    out = dedup_stories(items, title_getter=lambda e: e["title"])
    assert len(out) == 2


# ---------------------------------------------------------------------------
# Snippet vs full article
# ---------------------------------------------------------------------------

def test_evidence_kind_distinguishes_snippet_from_full_text():
    art = evidence_from_fetch({"url": "https://x.com/a", "title": "Full",
                               "content": "full text"})
    snip = evidence_from_search({"url": "https://y.com/b", "title": "Snip",
                                 "snippet": "short"})
    assert is_full_article(art) and not is_full_article(snip)
    assert art["kind"] == "article" and snip["kind"] == "search"


def test_select_evidence_prefers_full_text_on_ties():
    snip = evidence_from_search({"title": "Same", "url": "https://a.com/1",
                                 "snippet": "s", "source_quality": 0.5})
    art = evidence_from_fetch({"title": "Same", "url": "https://b.com/2",
                               "content": "c"})
    art["source_quality"] = 0.5
    selected = select_evidence([snip, art], "same query", 1)
    assert selected[0]["url"] == "https://b.com/2"


# ---------------------------------------------------------------------------
# Universe scoping
# ---------------------------------------------------------------------------

def test_query_universe_detection():
    assert requested_universe(FTSE_RANK_Q) == "ftse"
    assert requested_universe("Who is the CEO of HSBC?") is None
    assert mentions_universe("The FTSE 100 rose today", "ftse")
    assert mentions_universe("A UK-listed group", "ftse")
    assert not mentions_universe("The largest US companies", "ftse")


def _finding(title: str, relevance: float = 0.8) -> QueryFinding:
    return QueryFinding(
        title=title, url="https://example.com/x", source="example.com",
        published_at=None, relevance=relevance, key_finding=title,
        significance="", impact=ImpactLevel.MEDIUM, sentiment=Sentiment.NEUTRAL,
        reason="", confidence=0.6,
    )


def test_findings_filtered_to_requested_universe():
    findings = [
        _finding("Top 15 FTSE 100 companies by market cap"),
        _finding("Largest US companies by market cap"),
        _finding("Global megacaps ranked"),
    ]
    kept = filter_findings_for_universe(findings, FTSE_RANK_Q)
    assert [f.title for f in kept] == ["Top 15 FTSE 100 companies by market cap"]


# ---------------------------------------------------------------------------
# Cross-section consistency / verification
# ---------------------------------------------------------------------------

def _market_cap_answer() -> QueryAnswer:
    return QueryAnswer(
        query=FTSE_RANK_Q, task_type=TaskType.GENERAL,
        answer=("The top FTSE companies ranked by market cap are: "
                "1. Shell (£150B), 2. HSBC (£120B)."),
        key_points=["Shell is the largest by market cap."],
        confidence=0.9, verified=True,
    )


def test_verify_discloses_contradictory_market_caps():
    answer = _market_cap_answer()
    evidence = [
        {"title": "A", "url": "https://finance.yahoo.com/a", "kind": "search",
         "text": "Shell market cap £150.0 billion as of 2026-10-01"},
        {"title": "B", "url": "https://www.bloomberg.com/b", "kind": "search",
         "text": "Shell market cap £120.0 billion as of 2026-10-01"},
        {"title": "C", "url": "https://www.londonstockexchange.com/c",
         "kind": "search",
         "text": "HSBC market cap £120.0 billion as of 2026-10-01"},
    ]
    added = verify_answer(answer, query=FTSE_RANK_Q, evidence=evidence)
    assert any("conflicting" in l.lower() and "shell" in l.lower()
               for l in added)
    assert answer.confidence <= 0.5


def test_verify_rejects_market_cap_ranking_without_usable_values():
    answer = _market_cap_answer()
    evidence = [{"title": "Yahoo", "url": "https://finance.yahoo.com/x",
                 "text": "AstraZeneca 238.677", "kind": "search"}]
    added = verify_answer(answer, query=FTSE_RANK_Q, evidence=evidence)
    assert added
    assert not answer.verified
    assert answer.confidence == 0.0
    assert any("market-cap" in l.lower() for l in answer.limitations)


def test_response_parser_enforces_market_cap_consistency():
    answer = _market_cap_answer()
    evidence = [{"title": "Yahoo", "url": "https://finance.yahoo.com/x",
                 "text": "AstraZeneca 238.677", "kind": "search"}]
    out = ResponseParser.validate(
        answer, {"https://finance.yahoo.com/x"},
        query=FTSE_RANK_Q, evidence=evidence, max_results=2)
    assert not out.verified
    assert out.confidence == 0.0
    assert any("market-cap" in l.lower() for l in out.limitations)


def test_verify_flags_off_universe_answer():
    answer = QueryAnswer(
        query="Top FTSE companies", task_type=TaskType.GENERAL,
        answer="The largest US-listed companies are Apple and Microsoft.",
        key_points=["Apple leads."], confidence=0.8, verified=True)
    evidence = [{"title": "US megacaps",
                 "url": "https://us.example/x",
                 "text": "Apple and Microsoft lead global market value.",
                 "kind": "search"}]
    verify_answer(answer, query=FTSE_RANK_Q, evidence=evidence)
    assert any("FTSE" in l for l in answer.limitations)
    assert answer.confidence <= 0.4


def test_verified_answer_keeps_confidence_when_evidence_supports_it():
    answer = QueryAnswer(
        query="Who is the CEO of HSBC?", task_type=TaskType.FACTUAL_LOOKUP,
        answer="Georges Elhedery is the Group Chief Executive of HSBC.",
        key_points=["HSBC appointed Georges Elhedery."],
        confidence=0.9, verified=True)
    evidence = [{"title": "HSBC", "url": "https://hsbc.com/x",
                 "text": "HSBC appointed Georges Elhedery as Group Chief "
                         "Executive.", "kind": "article"}]
    added = verify_answer(answer, query="Who is the CEO of HSBC?",
                          evidence=evidence)
    assert added == []
    assert answer.verified and answer.confidence == 0.9


def test_verify_never_raises_confidence():
    answer = QueryAnswer(query="q", task_type=TaskType.GENERAL,
                         answer="A normal sentence.", key_points=[],
                         confidence=0.2, verified=False)
    verify_answer(answer, query="q", evidence=[])
    assert answer.confidence == 0.0  # unverified stays at zero


@pytest.mark.asyncio
async def test_unverified_market_cap_ranking_withholds_findings(
        tmp_path, monkeypatch):
    """An unverified market-cap ranking must not show unrelated ranked
    findings as a substitute."""
    from agent.research_agent import AgentResult
    from nodes import runner as runner_mod
    from utils.cache import CacheManager
    from utils.models import ResearchPlan

    settings = Settings(
        output_dir=tmp_path, cache_dir=tmp_path / "cache",
        ollama_api_key="test-key", ollama_base_url="https://ollama.example.com",
        ollama_model="qwen3:8b", searxng_base_url="http://localhost:8888",
    )
    settings.cache_dir.mkdir(exist_ok=True)

    plan = ResearchPlan(task_type=TaskType.GENERAL, intent_summary="rank",
                        detailed=True, max_tool_calls=3)
    answer = QueryAnswer.unverified(FTSE_RANK_Q, TaskType.GENERAL,
                                    ["no market-cap ranking evidence"])
    evidence = [{"title": "Yahoo", "url": "https://finance.yahoo.com/x",
                 "source": "finance.yahoo.com", "text": "AstraZeneca 238.677",
                 "kind": "search", "source_quality": 0.8}]
    result = AgentResult(
        query=FTSE_RANK_Q, plan=plan, answer=answer, findings=[],
        counts={"tool_calls": 1, "evidence_items": 1, "sources_cited": 0},
        warnings=[], tool_trace=[], evidence=evidence)

    async def fake_research(self, query, **kwargs):
        return result

    monkeypatch.setattr("agent.research_agent.ResearchAgent.research",
                        fake_research)

    called = {"n": 0}

    async def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("analyze_query must not run for a withheld ranking")

    monkeypatch.setattr("nodes.analysis.analyze_query", boom)

    report = await runner_mod.execute_query_research(
        FTSE_RANK_Q, settings, CacheManager(settings.cache_dir),
        run_id="run_withhold", max_results=2, agentic=True)

    assert report.findings == []
    assert called["n"] == 0
    assert any("withheld" in w.lower() for w in report.warnings)
