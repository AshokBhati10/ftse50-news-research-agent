"""Regression tests for the Maximum-results contract.

These pin the *global distinct-articles* semantics of ``max_results``:
it caps the analysed evidence set (and therefore cited sources) for the whole
run, is applied after URL/story deduplication, and never shrinks how many
searches run or how many candidates each search retrieves.

Model calls and network access are stubbed; scheduling, de-duplication,
selection, the cap arithmetic, planner diversification and API propagation are
all real.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from agent.planner import diversify_queries
from agent.research_agent import ResearchAgent, ResearchAnalyzer
from agent.tools import SearchTool
from api.app import create_app
from api.services.runs import RunManager
from config.settings import Settings
from nodes.output import build_query_report
from nodes.search import TopicSearchOutcome
from utils.models import (
    AgentStep,
    QueryAnswer,
    ResearchPlan,
    SourceRecord,
    TaskType,
)


def _settings(**overrides):
    base = dict(
        ollama_api_key="test-key-DO-NOT-LEAK",
        ollama_base_url="https://ollama.example.com",
        ollama_model="qwen3:8b",
        searxng_base_url="http://localhost:8888",
    )
    base.update(overrides)
    return Settings(**base)


def _result(title: str, url: str, *, quality: float = 0.5,
            snippet: str = "snippet text") -> dict:
    return {
        "title": title,
        "url": url,
        "source": "example.com",
        "published_at": None,
        "snippet": snippet,
        "source_quality": quality,
        "is_homepage": False,
    }


def _plan(queries: list[str], *, task=TaskType.NEWS_RESEARCH,
          max_tool_calls: int = 5) -> ResearchPlan:
    return ResearchPlan(
        task_type=task,
        intent_summary="Find recent developments.",
        tools_allowed=["web_search", "fetch_article"],
        search_queries=queries,
        max_tool_calls=max_tool_calls,
    )


def _answer_from(evidence: list[dict], *, verified: bool = True) -> QueryAnswer:
    sources = [
        SourceRecord(title=e.get("title", ""), url=e.get("url", ""),
                     supports_claims=[])
        for e in evidence
    ]
    return QueryAnswer(
        query="q", task_type=TaskType.NEWS_RESEARCH,
        answer="A concise answer.", key_points=["k"], sources=sources,
        confidence=0.8, verified=verified,
    )


def _wire(monkeypatch, plan, search_results, *, capture=None):
    async def fake_search(self, args):
        return {"ok": True, "results": search_results.get(args.query, []),
                "providers": ["searxng"], "notes": []}

    async def fake_next_step(self, q, p, t, c):
        return AgentStep(action="finish", reason="enough")

    async def fake_synth(self, q, p, e):
        if capture is not None:
            capture["evidence"] = list(e)
        return _answer_from(e)

    monkeypatch.setattr("agent.planner.QueryPlanner.plan",
                        lambda self, query: _coro(plan))
    monkeypatch.setattr(SearchTool, "run", fake_search)
    monkeypatch.setattr(ResearchAgent, "_next_step", fake_next_step)
    monkeypatch.setattr(ResearchAnalyzer, "synthesize", fake_synth)


def _coro(value):
    async def _inner():
        return value
    return _inner()


# ---------------------------------------------------------------------------
# Global cap: evidence items and cited sources never exceed max_results
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cap_of_20_keeps_all_fewer_candidates(monkeypatch):
    """A generous cap never pads or drops: 6 distinct results -> 6 items."""
    plan = _plan(["q1", "q2"])
    results = {
        "q1": [_result(f"Story {c}", f"https://{c}.example/1") for c in "ABC"],
        "q2": [_result(f"Report {c}", f"https://{c}.example/2") for c in "DEF"],
    }
    _wire(monkeypatch, plan, results, capture={})
    agent = ResearchAgent(_settings(), cache=None, run_id="cap20")
    result = await agent.research("recent developments", max_results=20)
    assert result.counts["max_results"] == 20
    assert result.counts["evidence_items"] == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [1, 2, 5])
async def test_global_cap_applies_across_multiple_searches(monkeypatch, cap):
    """Six distinct results across three queries collapse to at most ``cap``."""
    plan = _plan(["q1", "q2", "q3"])
    results = {
        "q1": [_result("Story A", "https://a.example/1"),
               _result("Story B", "https://b.example/1")],
        "q2": [_result("Story C", "https://c.example/1"),
               _result("Story D", "https://d.example/1")],
        "q3": [_result("Story E", "https://e.example/1"),
               _result("Story F", "https://f.example/1")],
    }
    capture: dict = {}
    _wire(monkeypatch, plan, results, capture=capture)

    agent = ResearchAgent(_settings(), cache=None, run_id=f"cap{cap}")
    result = await agent.research("recent developments", max_results=cap)

    # All three planned searches ran (breadth preserved) ...
    assert result.counts["tool_calls"] == 3
    assert result.counts["candidates_considered"] == 6
    # ... but only ``cap`` distinct articles are analysed and citable.
    assert result.counts["evidence_items"] == cap
    assert len(result.evidence) == cap
    assert result.counts["sources_cited"] <= cap
    assert len(capture["evidence"]) == cap


@pytest.mark.asyncio
async def test_per_search_limit_is_independent_of_global_cap(monkeypatch):
    """A cap of 1 must not shrink the per-search retrieval to 1."""
    seen: list[int] = []

    async def fake_search(self, args):
        seen.append(args.max_results)
        return {"ok": True, "results": [_result("Only", "https://a.example/1")],
                "providers": ["searxng"], "notes": []}

    async def fake_next_step(self, q, p, t, c):
        return AgentStep(action="finish", reason="enough")

    async def fake_synth(self, q, p, e):
        return _answer_from(e)

    monkeypatch.setattr("agent.planner.QueryPlanner.plan",
                        lambda self, query: _coro(_plan(["q1"])))
    monkeypatch.setattr(SearchTool, "run", fake_search)
    monkeypatch.setattr(ResearchAgent, "_next_step", fake_next_step)
    monkeypatch.setattr(ResearchAnalyzer, "synthesize", fake_synth)

    agent = ResearchAgent(_settings(search_results_per_query=10),
                          cache=None, run_id="indep")
    await agent.research("q", max_results=1)

    assert seen and seen[0] == 10  # not 1


@pytest.mark.asyncio
async def test_max_results_normalized_out_of_range(monkeypatch):
    plan = _plan(["q1"])
    _wire(monkeypatch, plan, {"q1": [_result("A", "https://a.example/1")]})
    agent = ResearchAgent(_settings(), cache=None, run_id="norm")

    capped = await agent.research("q", max_results=999)
    assert capped.counts["max_results"] == 50

    floored = await agent.research("q", max_results=0)
    assert floored.counts["max_results"] == 1


# ---------------------------------------------------------------------------
# URL and story de-duplication before counting distinct results
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_urls_across_searches_counted_once(monkeypatch):
    plan = _plan(["q1", "q2"])
    same = _result("Syndicated story", "https://news.example/story?utm_source=x")
    repeat = _result("Syndicated story", "https://www.news.example/story")
    _wire(monkeypatch, plan, {"q1": [same], "q2": [repeat]})

    agent = ResearchAgent(_settings(), cache=None, run_id="urldedup")
    result = await agent.research("q", max_results=5)

    assert result.counts["candidates_considered"] == 1
    assert result.counts["evidence_items"] == 1


@pytest.mark.asyncio
async def test_syndicated_story_deduped_across_domains(monkeypatch):
    plan = _plan(["q1", "q2"])
    headline = "AstraZeneca to buy Summit Therapeutics in $2bn deal"
    r1 = _result(headline, "https://reuters.com/x", quality=0.8)
    r2 = _result(headline, "https://someblog.com/x", quality=0.4)
    _wire(monkeypatch, plan, {"q1": [r1], "q2": [r2]})

    agent = ResearchAgent(_settings(), cache=None, run_id="storydedup")
    result = await agent.research("AstraZeneca latest developments", max_results=5)

    # Two URLs, two domains, one story -> one analysed article.
    assert result.counts["candidates_considered"] == 2
    assert result.counts["evidence_items"] == 1
    assert result.evidence[0]["url"] == "https://reuters.com/x"  # higher quality wins


# ---------------------------------------------------------------------------
# Distinct developments discovered via distinct queries
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_paraphrased_duplicates_do_not_crowd_out_second_development(monkeypatch):
    """Two paraphrases of one deal must not consume both slots for a cap of 2."""
    plan = _plan(["q1", "q2", "q3"])
    summit1 = _result("AstraZeneca invests $2bn in Summit Therapeutics",
                      "https://a.example/summit1", quality=0.9)
    summit2 = _result("AstraZeneca to invest $2bn in Summit Therapeutics deal",
                      "https://b.example/summit2", quality=0.8)
    mass = _result(
        "AstraZeneca to invest over $1bn in Massachusetts operations",
        "https://c.example/mass", quality=0.6)
    _wire(monkeypatch, plan, {"q1": [summit1, summit2], "q2": [mass],
                              "q3": []})

    agent = ResearchAgent(_settings(), cache=None, run_id="diversity")
    result = await agent.research("AstraZeneca latest developments",
                                  max_results=2)

    urls = {e["url"] for e in result.evidence}
    assert "https://c.example/mass" in urls  # distinct development retained
    assert len(urls) == 2


@pytest.mark.asyncio
async def test_distinct_developments_from_different_queries_survive(monkeypatch):
    plan = _plan(["AstraZeneca announcement",
                  "AstraZeneca acquisition investment"])
    summit = _result("AstraZeneca invests $2bn in Summit Therapeutics",
                     "https://reuters.com/summit")
    mass = _result("AstraZeneca to invest over $1bn in Massachusetts operations",
                   "https://ft.com/massachusetts")
    _wire(monkeypatch, plan, {
        "AstraZeneca announcement": [summit],
        "AstraZeneca acquisition investment": [mass],
    })

    agent = ResearchAgent(_settings(), cache=None, run_id="distinct")
    result = await agent.research("AstraZeneca latest developments",
                                  max_results=2)

    urls = {e["url"] for e in result.evidence}
    assert urls == {"https://reuters.com/summit", "https://ft.com/massachusetts"}


# ---------------------------------------------------------------------------
# Planner query diversification (generic, no company hardcoding)
# ---------------------------------------------------------------------------

def test_diversify_queries_drops_near_identical_and_tops_up_news():
    qs = diversify_queries(
        ["latest developments semiconductor stocks",
         "latest developments in semiconductor stocks",
         "semiconductor stocks latest news"],
        "latest developments in semiconductor stocks",
        TaskType.NEWS_RESEARCH,
    )
    assert len(qs) == len(set(qs))
    assert len(qs) <= 5
    # A thin news plan is topped up with distinct aspect queries.
    assert len(qs) >= 2
    assert any("semiconductor stocks" in q for q in qs)


def test_diversify_queries_is_bounded_and_keeps_factual_queries():
    qs = diversify_queries(["Who is the CEO of HSBC?"],
                           "Who is the CEO of HSBC?", TaskType.FACTUAL_LOOKUP)
    assert qs == ["Who is the CEO of HSBC?"]  # factual lookups aren't expanded


# ---------------------------------------------------------------------------
# API: max_results reaches the backend pipeline
# ---------------------------------------------------------------------------

@pytest.fixture
def contract_client(tmp_path, monkeypatch):
    out = tmp_path / "output"
    out.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    settings = Settings(
        output_dir=out, cache_dir=cache,
        ollama_api_key="test-key", ollama_base_url="https://ollama.example.com",
        ollama_model="qwen3:8b", searxng_base_url="http://localhost:8888",
    )
    monkeypatch.setattr("config.settings.Settings", lambda *a, **k: settings)

    received: dict = {}

    async def fake_query_run(query, settings, cache, *, run_id="",
                             max_results=20, time_range="month", fresh=False,
                             agentic=True, on_stage=None):
        received["max_results"] = max_results
        outcome = TopicSearchOutcome(query=query, queries_used=[query],
                                     articles=[], providers_used=["searxng"],
                                     time_range=time_range)
        return build_query_report(
            query=query, run_id=run_id, started_at=datetime.now(timezone.utc),
            outcome=outcome, findings=[],
            counts={"max_results": max_results, "collected": 0, "extracted": 0,
                    "duplicates_removed": 0, "analyzed": 0},
            warnings=[], settings=settings)

    monkeypatch.setattr("nodes.runner.execute_query_research", fake_query_run)

    def fake_write(report, output_dir):
        stamp = report.generated_at.strftime("%Y-%m-%d")
        jp = output_dir / f"report_{stamp}.json"
        jp.write_text(report.model_dump_json(), encoding="utf-8")
        mp = output_dir / f"report_{stamp}.md"
        mp.write_text("# r\n", encoding="utf-8")
        return jp, mp

    monkeypatch.setattr("nodes.output.write_query_reports", fake_write)

    app = create_app()
    with TestClient(app) as client:
        app.state.settings = settings
        app.state.run_manager = RunManager(cache / "runs")
        yield client, received


def _wait(client, run_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/research-runs/{run_id}").json()
        if body["status"] not in ("queued", "running"):
            return body
        time.sleep(0.1)
    raise AssertionError("run did not settle")


def test_api_max_results_reaches_pipeline(contract_client):
    client, received = contract_client
    resp = client.post("/api/research-runs",
                       json={"query": "banks", "max_results": 2})
    assert resp.status_code == 201, resp.text
    body = _wait(client, resp.json()["run_id"])
    assert body["request"]["max_results"] == 2
    assert received["max_results"] == 2  # propagated into the pipeline


def test_api_max_results_defaults_to_20(contract_client):
    client, received = contract_client
    resp = client.post("/api/research-runs", json={"query": "banks"})
    assert resp.status_code == 201, resp.text
    body = _wait(client, resp.json()["run_id"])
    assert body["request"]["max_results"] == 20
    assert received["max_results"] == 20


@pytest.mark.parametrize("bad", [0, 51, -3])
def test_api_rejects_out_of_range_max_results(contract_client, bad):
    client, _ = contract_client
    resp = client.post("/api/research-runs",
                       json={"query": "banks", "max_results": bad})
    assert resp.status_code in (400, 422)
