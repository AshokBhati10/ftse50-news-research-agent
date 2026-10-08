"""Tests for nodes/analysis.py — prompt, selection, validation, heuristics, retries."""

import pytest
from pydantic import ValidationError

from config.settings import Settings
from nodes import analysis as analysis_mod
from nodes.analysis import (
    AnalysisError,
    analyze_company,
    build_analysis_prompt,
    heuristic_analyze,
    select_top_n,
)
from utils.models import (
    AnalysisResult,
    Article,
    ArticleScore,
    Company,
    ImpactLevel,
    Sentiment,
)


def _company() -> Company:
    return Company(name="AstraZeneca", ticker="AZN.L", market_cap=1e11)


def _articles() -> list[Article]:
    return [
        Article(company="AstraZeneca", ticker="AZN.L",
                title="Merger announced", url="https://reuters.com/1",
                snippet="acquisition deal", source_quality=0.8),
        Article(company="AstraZeneca", ticker="AZN.L",
                title="Dividend reminder", url="https://ft.com/2",
                snippet="dividend payout", source_quality=0.8),
        Article(company="AstraZeneca", ticker="AZN.L",
                title="CEO interview", url="https://bbc.com/3",
                snippet="general comments", source_quality=0.6),
    ]


def _scores() -> AnalysisResult:
    return AnalysisResult(scores=[
        ArticleScore(index=0, impact=ImpactLevel.HIGH, sentiment=Sentiment.POSITIVE,
                     reason="Major acquisition, clearly market-moving.", confidence=0.9),
        ArticleScore(index=1, impact=ImpactLevel.LOW, sentiment=Sentiment.NEUTRAL,
                     reason="Routine dividend notice.", confidence=0.8),
        ArticleScore(index=2, impact=ImpactLevel.MEDIUM, sentiment=Sentiment.NEUTRAL,
                     reason="CEO commentary, no new numbers.", confidence=0.6),
    ])


def _live_settings(**overrides) -> Settings:
    s = Settings()
    s.ollama_api_key = "test-key"
    s.ollama_base_url = "https://ollama.test/v1"
    s.ollama_model = "test-model"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _fast_retries(monkeypatch):
    """Patch retry_async to use tiny delays so retry tests run fast."""
    real_retry = analysis_mod.retry_async

    async def fast_retry(fn, **kw):
        kw["base_delay_s"] = 0.01
        kw["max_delay_s"] = 0.05
        return await real_retry(fn, **kw)

    monkeypatch.setattr(analysis_mod, "retry_async", fast_retry)


def test_prompt_contains_indexed_articles():
    prompt = build_analysis_prompt(_company(), _articles())
    assert "AstraZeneca (AZN.L)" in prompt
    assert "[0] Merger announced" in prompt
    assert "[2] CEO interview" in prompt


def test_select_top_n_orders_by_impact_then_confidence():
    ranked = select_top_n(_company(), _articles(), _scores(), top_n=3)
    assert [r.impact for r in ranked] == [ImpactLevel.HIGH, ImpactLevel.MEDIUM, ImpactLevel.LOW]
    assert ranked[0].title == "Merger announced"
    # Evidence is preserved on every finding.
    assert ranked[0].url == "https://reuters.com/1"
    assert ranked[0].reason.startswith("Major acquisition")


def test_select_top_n_rejects_unmatched_scores():
    with pytest.raises(AnalysisError):
        select_top_n(_company(), _articles(), AnalysisResult(scores=[
            ArticleScore(index=9, impact=ImpactLevel.LOW, sentiment=Sentiment.NEUTRAL,
                         reason="unknown article", confidence=0.5),
        ]), top_n=3)


def test_malformed_llm_output_rejected():
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate({"scores": [{"index": 0, "impact": "Extreme"}]})
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate({"scores": []})
    with pytest.raises(ValidationError):
        ArticleScore.model_validate({"index": 0, "impact": "High",
                                     "sentiment": "Positive",
                                     "reason": "x" * 500, "confidence": 0.9})


def test_heuristic_ranks_merger_above_dividend():
    ranked = heuristic_analyze(_company(), _articles(), top_n=3)
    assert ranked[0].title == "Merger announced"
    assert ranked[0].impact == ImpactLevel.HIGH
    assert ranked[1].title == "Dividend reminder"
    assert ranked[1].impact == ImpactLevel.MEDIUM
    assert ranked[2].title == "CEO interview"
    assert ranked[2].impact == ImpactLevel.LOW


def test_heuristic_is_deterministic():
    first = [r.model_dump() for r in heuristic_analyze(_company(), _articles())]
    second = [r.model_dump() for r in heuristic_analyze(_company(), _articles())]
    assert first == second


@pytest.mark.asyncio
async def test_analyze_company_retries_then_succeeds(monkeypatch):
    """Transient LLM failures are retried; eventual success is returned."""
    settings = _live_settings()
    calls = {"n": 0}

    class FakeRun:
        output = _scores()

    class FakeAgent:
        async def run(self, prompt):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("transient 502")
            return FakeRun()

    monkeypatch.setattr(analysis_mod, "build_analysis_agent", lambda s: FakeAgent())
    _fast_retries(monkeypatch)

    ranked = await analyze_company(_company(), _articles(), settings, cache=None)
    assert calls["n"] == 3
    assert ranked[0].impact == ImpactLevel.HIGH


@pytest.mark.asyncio
async def test_analyze_company_gives_up_after_bounded_retries(monkeypatch):
    settings = _live_settings(llm_max_retries=2)

    class FakeAgent:
        async def run(self, prompt):
            raise RuntimeError("always down")

    monkeypatch.setattr(analysis_mod, "build_analysis_agent", lambda s: FakeAgent())
    _fast_retries(monkeypatch)

    with pytest.raises(AnalysisError):
        await analyze_company(_company(), _articles(), settings, cache=None)


@pytest.mark.asyncio
async def test_analyze_company_requires_llm_config():
    with pytest.raises(RuntimeError, match="OLLAMA_API_KEY"):
        await analyze_company(_company(), _articles(), Settings(), cache=None)
