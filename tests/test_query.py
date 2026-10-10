"""Tests for query-driven research: search, analysis, reports, runner, API.

External services (SearXNG, You.com, crawl targets, Ollama) are stubbed;
what is real: query validation, cache-key construction, fresh-search bypass,
prompt construction, deterministic ranking, report metadata, run lifecycle,
SSE stage events, cancellation, and the report library.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.services.runs import RunManager
from config.settings import Settings
from nodes.analysis import (
    AnalysisError,
    build_query_analysis_prompt,
    rank_query_findings,
)
from nodes.output import build_query_report, render_query_markdown, write_query_reports
from nodes.search import (
    TopicSearchOutcome,
    build_topic_queries,
    normalize_query,
    search_topic,
    topic_cache_key,
)
from utils.models import (
    Article,
    ImpactLevel,
    QueryAnalysisResult,
    QueryArticleScore,
    QueryFinding,
    Sentiment,
)


def _settings(tmp_path, **overrides):
    out = tmp_path / "output"
    out.mkdir(exist_ok=True)
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    overrides.setdefault("ollama_api_key", "test-key-DO-NOT-LEAK")
    return Settings(
        output_dir=out,
        cache_dir=cache,
        ollama_base_url="https://ollama.example.com",
        ollama_model="qwen3:8b",
        searxng_base_url="http://localhost:8888",
        **overrides,
    )


def _article(i: int, **kw) -> Article:
    return Article(
        company="", ticker="",
        title=f"Headline {i}", url=f"https://example.com/news/{i}",
        source="example.com", snippet=f"Snippet {i}",
        search_provider="searxng", **kw,
    )


# ---------------------------------------------------------------------------
# Query construction / cache keys
# ---------------------------------------------------------------------------

def test_build_topic_queries_raw_first_and_bounded():
    qs = build_topic_queries("How are interest rate changes affecting UK banks?")
    assert qs[0] == "How are interest rate changes affecting UK banks?"
    assert len(qs) <= 3
    assert len(set(qs)) == len(qs)


def test_build_topic_queries_no_duplicate_when_newsy():
    qs = build_topic_queries("latest news on semiconductors")
    assert qs.count("latest news on semiconductors") == 1
    assert len(qs) <= 3


def test_topic_cache_keys_isolate_queries():
    a = topic_cache_key("semiconductor stocks", "month", 20)
    b = topic_cache_key("oil prices", "month", 20)
    c = topic_cache_key("  Semiconductor   Stocks ", "month", 20)  # normalization
    d = topic_cache_key("semiconductor stocks", "week", 20)
    e = topic_cache_key("semiconductor stocks", "month", 10)
    assert a != b
    assert a == c  # normalized identically
    assert a != d  # time range matters
    assert a != e  # limit matters


def test_normalize_query():
    assert normalize_query("  Foo   BAR\nbaz ") == "foo bar baz"


# ---------------------------------------------------------------------------
# search_topic — fresh bypass, failure handling, validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_topic_empty_query_rejected(tmp_path):
    with pytest.raises(ValueError):
        await search_topic("   ", _settings(tmp_path))


@pytest.mark.asyncio
async def test_search_topic_bad_time_range_rejected(tmp_path):
    with pytest.raises(ValueError):
        await search_topic("banks", _settings(tmp_path), time_range="decade")


@pytest.mark.asyncio
async def test_search_topic_uses_raw_query_first(tmp_path, monkeypatch):
    seen: list[str] = []

    async def fake_searxng(query, settings, max_results=10, time_range="month"):
        seen.append(query)
        return [_article(1)]

    monkeypatch.setattr("nodes.search.searxng_search", fake_searxng)
    settings = _settings(tmp_path)
    outcome = await search_topic("interest rates UK banks", settings, cache=None,
                                 max_results=5)
    assert seen[0] == "interest rates UK banks"  # raw query, unmodified
    assert outcome.query == "interest rates UK banks"
    assert outcome.providers_used == ["searxng"]
    assert outcome.time_range == "month"


@pytest.mark.asyncio
async def test_search_topic_provider_failure_recorded_not_raised(tmp_path, monkeypatch):
    async def boom(*a, **k):
        from nodes.search import SearchError
        raise SearchError("down")

    monkeypatch.setattr("nodes.search.searxng_search", boom)
    settings = _settings(tmp_path)  # no you.com key
    outcome = await search_topic("banks", settings, cache=None, max_results=5)
    assert outcome.articles == []
    assert "SearXNG failed" in outcome.notes
    assert "YOU_API_KEY not configured" in outcome.notes


@pytest.mark.asyncio
async def test_search_topic_fresh_bypasses_cache(tmp_path, monkeypatch):
    from utils.cache import CacheManager
    settings = _settings(tmp_path)
    cache = CacheManager(settings.cache_dir)
    calls = {"n": 0}

    async def fake_searxng(query, settings, max_results=10, time_range="month"):
        calls["n"] += 1
        return [_article(calls["n"])]

    monkeypatch.setattr("nodes.search.searxng_search", fake_searxng)
    first = await search_topic("ev industry", settings, cache, max_results=5)
    assert not first.from_cache
    assert calls["n"] >= 1

    second = await search_topic("ev industry", settings, cache, max_results=5)
    assert second.from_cache  # cache hit, no new search call
    n_after_hit = calls["n"]

    third = await search_topic("ev industry", settings, cache, max_results=5, fresh=True)
    assert not third.from_cache  # fresh bypasses the cached entry
    assert calls["n"] > n_after_hit


# ---------------------------------------------------------------------------
# Analysis: prompt, ranking
# ---------------------------------------------------------------------------

def test_query_prompt_contains_original_query():
    prompt = build_query_analysis_prompt("Latest developments in EV batteries",
                                         [_article(1)])
    assert "Latest developments in EV batteries" in prompt
    assert "Headline 1" in prompt


def _scores(*relevances: float) -> QueryAnalysisResult:
    return QueryAnalysisResult(scores=[
        QueryArticleScore(
            index=i, relevance=r, key_finding="k", significance="s",
            impact=ImpactLevel.LOW, sentiment=Sentiment.NEUTRAL,
            reason="r", confidence=0.5,
        )
        for i, r in enumerate(relevances)
    ])


def test_rank_query_findings_relevance_dominates():
    arts = [_article(0), _article(1), _article(2)]
    arts[1].source_quality = 0.99  # popular source must not outrank relevance
    arts[0].source_quality = 0.10
    ranked = rank_query_findings(arts, _scores(0.95, 0.20, 0.60), top_n=3)
    assert [f.relevance for f in ranked] == [0.95, 0.60, 0.20]
    assert ranked[0].title == "Headline 0"


def test_rank_query_findings_impact_breaks_relevance_ties():
    arts = [_article(0), _article(1)]
    result = QueryAnalysisResult(scores=[
        QueryArticleScore(index=0, relevance=0.7, key_finding="k", significance="s",
                          impact=ImpactLevel.LOW, sentiment=Sentiment.NEUTRAL,
                          reason="r", confidence=0.5),
        QueryArticleScore(index=1, relevance=0.7, key_finding="k", significance="s",
                          impact=ImpactLevel.HIGH, sentiment=Sentiment.NEUTRAL,
                          reason="r", confidence=0.5),
    ])
    ranked = rank_query_findings(arts, result, top_n=2)
    assert ranked[0].title == "Headline 1"  # high impact wins the tie


def test_rank_query_findings_empty_scores_raises():
    with pytest.raises(AnalysisError):
        rank_query_findings([_article(0)], QueryAnalysisResult(scores=[
            QueryArticleScore(index=9, relevance=0.5, key_finding="k", significance="s",
                              impact=ImpactLevel.LOW, sentiment=Sentiment.NEUTRAL,
                              reason="r", confidence=0.5),
        ]), top_n=3)


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _finding(i: int, relevance: float = 0.8) -> QueryFinding:
    return QueryFinding(
        title=f"Finding {i}", url=f"https://example.com/{i}", source="example.com",
        published_at=None, relevance=relevance, key_finding=f"Key {i}",
        significance=f"Sig {i}", impact=ImpactLevel.MEDIUM,
        sentiment=Sentiment.NEUTRAL, reason="Reason", confidence=0.7,
        source_quality=0.6, evidence_excerpt="Evidence",
    )


def test_query_report_preserves_query_metadata(tmp_path):
    settings = _settings(tmp_path)
    outcome = TopicSearchOutcome(
        query="semiconductor stocks", queries_used=["semiconductor stocks"],
        articles=[], providers_used=["searxng"], time_range="month",
        fresh_search=True, from_cache=False,
    )
    started = datetime.now(timezone.utc)
    report = build_query_report(
        query="semiconductor stocks", run_id="run_test1", started_at=started,
        outcome=outcome, findings=[_finding(1)],
        counts={"collected": 8, "extracted": 5, "duplicates_removed": 2,
                "analyzed": 5, "max_results": 20},
        warnings=["SearXNG refined failed: boom"],
        settings=settings,
    )
    assert report.report_type == "query"
    assert report.query == "semiconductor stocks"
    assert report.run_id == "run_test1"
    assert report.search_config["fresh_search"] is True
    assert report.search_config["time_range"] == "month"
    assert report.counts["collected"] == 8
    assert report.warnings == ["SearXNG refined failed: boom"]

    md = render_query_markdown(report)
    for section in ("**Query:**", "Research metadata", "Executive summary",
                    "Key findings", "Ranked results", "Source links",
                    "Limitations and warnings"):
        assert section in md
    assert "semiconductor stocks" in md

    jp, mp = write_query_reports(report, settings.output_dir)
    payload = json.loads(jp.read_text(encoding="utf-8"))
    assert payload["report_type"] == "query"
    assert payload["query"] == "semiconductor stocks"
    assert len(payload["findings"]) == 1


def test_query_report_zero_results_is_honest(tmp_path):
    settings = _settings(tmp_path)
    outcome = TopicSearchOutcome(query="obscure topic", articles=[],
                                 providers_used=[], time_range="week")
    report = build_query_report(
        query="obscure topic", run_id="run_test2",
        started_at=datetime.now(timezone.utc), outcome=outcome, findings=[],
        counts={"collected": 0, "extracted": 0, "duplicates_removed": 0,
                "analyzed": 0, "max_results": 20},
        warnings=["No extractable articles found for this query."],
        settings=settings,
    )
    assert report.findings == []
    md = render_query_markdown(report)
    assert "No findings" in md
    assert "No extractable articles found" in md


# ---------------------------------------------------------------------------
# Runner: query pipeline with stubbed stages
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_query_research_zero_results(tmp_path, monkeypatch):
    from nodes.runner import execute_query_research

    async def fake_search(query, settings, cache, *, max_results=20,
                          time_range="month", fresh=False):
        return TopicSearchOutcome(query=query, articles=[], providers_used=[],
                                  time_range=time_range)

    async def fake_extract(articles, settings, cache=None):
        return []

    class FakeDeduper:
        async def deduplicate_async(self, articles):
            return articles

    monkeypatch.setattr("nodes.search.search_topic", fake_search)
    monkeypatch.setattr("nodes.crawl.extract_articles", fake_extract)
    monkeypatch.setattr("nodes.deduplication.build_deduper", lambda s: FakeDeduper())

    stages: list[str] = []
    settings = _settings(tmp_path, ollama_api_key="")  # no LLM: must not be needed
    from utils.cache import CacheManager
    report = await execute_query_research(
        "some obscure topic", settings, CacheManager(settings.cache_dir),
        run_id="run_zero", max_results=10, agentic=False,
        on_stage=lambda s, info: stages.append(s),
    )
    assert report.findings == []
    assert report.query == "some obscure topic"
    assert report.counts["collected"] == 0
    assert any("No extractable articles" in w for w in report.warnings)
    assert stages == ["search", "extract", "deduplicate", "analyze", "report"]


@pytest.mark.asyncio
async def test_execute_query_research_stage_events_and_warnings(tmp_path, monkeypatch):
    from nodes.runner import execute_query_research

    async def fake_search(query, settings, cache, *, max_results=20,
                          time_range="month", fresh=False):
        return TopicSearchOutcome(
            query=query, articles=[_article(1), _article(2)],
            providers_used=["searxng"], time_range=time_range,
            notes="You.com fallback skipped: YOU_API_KEY not configured",
        )

    async def fake_extract(articles, settings, cache=None):
        for a in articles:
            a.extraction_status = "ok"
            a.content = "Body text " * 20
        return articles

    class FakeDeduper:
        async def deduplicate_async(self, articles):
            return articles

    async def fake_analyze(query, articles, settings, cache=None, agent=None, top_n=None):
        assert "obscure" in query  # the original query reaches the analyzer
        return [_finding(1)]

    monkeypatch.setattr("nodes.search.search_topic", fake_search)
    monkeypatch.setattr("nodes.crawl.extract_articles", fake_extract)
    monkeypatch.setattr("nodes.deduplication.build_deduper", lambda s: FakeDeduper())
    monkeypatch.setattr("nodes.analysis.analyze_query", fake_analyze)

    seen: list[tuple[str, dict]] = []
    settings = _settings(tmp_path)
    from utils.cache import CacheManager
    report = await execute_query_research(
        "some obscure topic", settings, CacheManager(settings.cache_dir),
        run_id="run_ev", agentic=False,
        on_stage=lambda s, info: seen.append((s, info)),
    )
    assert len(report.findings) == 1
    assert report.findings[0].key_finding == "Key 1"
    assert report.counts["collected"] == 2
    assert [s for s, _ in seen] == ["search", "extract", "deduplicate", "analyze", "report"]
    assert seen[0][1]["collected"] == 2
    assert any("YOU_API_KEY not configured" in w for w in report.warnings)


@pytest.mark.asyncio
async def test_execute_query_research_analysis_failure_keeps_partial(tmp_path, monkeypatch):
    from nodes.runner import execute_query_research

    async def fake_search(query, settings, cache, *, max_results=20,
                          time_range="month", fresh=False):
        return TopicSearchOutcome(query=query, articles=[_article(1)],
                                  providers_used=["searxng"], time_range=time_range)

    async def fake_extract(articles, settings, cache=None):
        for a in articles:
            a.extraction_status = "ok"
        return articles

    class FakeDeduper:
        async def deduplicate_async(self, articles):
            return articles

    async def boom(*a, **k):
        raise AnalysisError("LLM down")

    monkeypatch.setattr("nodes.search.search_topic", fake_search)
    monkeypatch.setattr("nodes.crawl.extract_articles", fake_extract)
    monkeypatch.setattr("nodes.deduplication.build_deduper", lambda s: FakeDeduper())
    monkeypatch.setattr("nodes.analysis.analyze_query", boom)

    settings = _settings(tmp_path)
    from utils.cache import CacheManager
    report = await execute_query_research(
        "q", settings, CacheManager(settings.cache_dir), run_id="run_p",
        agentic=False)
    assert report.findings == []
    assert any("LLM down" in w for w in report.warnings)  # honest, no crash


# ---------------------------------------------------------------------------
# API: query run validation + lifecycle
# ---------------------------------------------------------------------------

@pytest.fixture
def query_client(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr("config.settings.Settings", lambda *a, **k: settings)

    async def fake_query_run(query, settings, cache, *, run_id="",
                             max_results=20, time_range="month", fresh=False,
                             agentic=True, on_stage=None):
        for stage, info in [("search", {"collected": 2}),
                            ("extract", {"extracted": 2}),
                            ("deduplicate", {"candidates": 2}),
                            ("analyze", {"findings": 1}),
                            ("report", {"findings": 1})]:
            if on_stage:
                on_stage(stage, info)
            await asyncio.sleep(0)
        outcome = TopicSearchOutcome(
            query=query, queries_used=[query], articles=[],
            providers_used=["searxng"], time_range=time_range, fresh_search=fresh)
        return build_query_report(
            query=query, run_id=run_id, started_at=datetime.now(timezone.utc),
            outcome=outcome, findings=[_finding(1)],
            counts={"collected": 2, "extracted": 2, "duplicates_removed": 0,
                    "analyzed": 2, "max_results": max_results},
            warnings=[], settings=settings)

    monkeypatch.setattr("nodes.runner.execute_query_research", fake_query_run)

    def fake_write_query(report, output_dir):
        stamp = report.generated_at.strftime("%Y-%m-%d")
        jp = output_dir / f"report_{stamp}.json"
        jp.write_text(report.model_dump_json(), encoding="utf-8")
        mp = output_dir / f"report_{stamp}.md"
        mp.write_text("# query report\n", encoding="utf-8")
        return jp, mp

    monkeypatch.setattr("nodes.output.write_query_reports", fake_write_query)

    app = create_app()
    with TestClient(app) as client:
        app.state.settings = settings
        app.state.run_manager = RunManager(settings.cache_dir / "runs")
        yield client, settings, monkeypatch


def _wait_for(client, run_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/research-runs/{run_id}").json()
        if body["status"] not in ("queued", "running"):
            return body
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not settle")


@pytest.mark.parametrize("payload", [
    {"query": ""},
    {"query": "   "},
    {"query": "x" * 501},
    {"query": "banks", "max_results": 0},
    {"query": "banks", "max_results": 51},
    {"query": "banks", "time_range": "decade"},
    {"query": "banks", "tickers": ["AZN.L"]},
])
def test_query_run_rejects_invalid_input(query_client, payload):
    client, _, _ = query_client
    resp = client.post("/api/research-runs", json=payload)
    assert resp.status_code in (400, 422), (payload, resp.text)


def test_query_run_agentic_opt_out(query_client):
    client, _, _ = query_client
    resp = client.post("/api/research-runs", json={"query": "banks", "agentic": False})
    assert resp.status_code == 201, resp.text
    body = _wait_for(client, resp.json()["run_id"])
    assert body["status"] == "completed"
    assert body["request"]["agentic"] is False


def test_query_run_trims_query(query_client):
    client, _, _ = query_client
    resp = client.post("/api/research-runs", json={"query": "  uk banks  "})
    assert resp.status_code == 201, resp.text
    body = _wait_for(client, resp.json()["run_id"])
    assert body["status"] == "completed"
    assert body["mode"] == "query"
    assert body["query"] == "uk banks"


def test_query_run_lifecycle_and_report(query_client):
    client, _, _ = query_client
    resp = client.post("/api/research-runs", json={
        "query": "semiconductor stocks", "max_results": 10,
        "time_range": "week", "fresh_search": True,
    })
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["run_id"]
    body = _wait_for(client, run_id)
    assert body["status"] == "completed"
    assert body["stage"] == "done"
    assert body["articles_found"] == 1
    report_id = body["report_id"]
    assert report_id

    detail = client.get(f"/api/reports/{report_id}").json()
    assert detail["summary"]["report_type"] == "query"
    assert detail["query"] == "semiconductor stocks"
    assert len(detail["findings"]) == 1
    assert detail["findings"][0]["key_finding"] == "Key 1"
    assert detail["metadata"]["search_config"]["time_range"] == "week"
    assert detail["metadata"]["search_config"]["fresh_search"] is True


def test_query_run_stage_events_over_sse(query_client, monkeypatch):
    client, settings, _ = query_client

    async def slow_query_run(query, settings, cache, *, run_id="",
                             max_results=20, time_range="month", fresh=False,
                             agentic=True, on_stage=None):
        for stage in ("search", "extract", "deduplicate", "analyze", "report"):
            await asyncio.sleep(0.4)
            if on_stage:
                on_stage(stage, {})
        outcome = TopicSearchOutcome(query=query, articles=[],
                                     providers_used=["searxng"],
                                     time_range=time_range)
        return build_query_report(
            query=query, run_id=run_id, started_at=datetime.now(timezone.utc),
            outcome=outcome, findings=[],
            counts={"collected": 0, "extracted": 0, "duplicates_removed": 0,
                    "analyzed": 0, "max_results": max_results},
            warnings=[], settings=settings)

    monkeypatch.setattr("nodes.runner.execute_query_research", slow_query_run)
    resp = client.post("/api/research-runs", json={"query": "ev batteries"})
    assert resp.status_code == 201
    run_id = resp.json()["run_id"]

    stages: list[str] = []
    with client.stream("GET", f"/api/research-runs/{run_id}/events") as stream:
        for line in stream.iter_lines():
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
                if payload.get("type") == "stage":
                    stages.append(payload["stage"])
                if payload.get("type") in ("completed", "failed", "cancelled"):
                    break
    for expected in ("search", "extract", "deduplicate", "analyze", "report"):
        assert expected in stages


def test_query_run_requires_llm_config(query_client):
    client, settings, _ = query_client
    settings.ollama_api_key = ""
    resp = client.post("/api/research-runs", json={"query": "banks"})
    assert resp.status_code == 400
    assert "OLLAMA_API_KEY" in resp.json()["detail"]


def test_query_run_cancellation(query_client, monkeypatch):
    client, settings, mp = query_client

    async def slow_run(query, settings, cache, **kwargs):
        await asyncio.sleep(30)

    mp.setattr("nodes.runner.execute_query_research", slow_run)
    resp = client.post("/api/research-runs", json={"query": "slow query"})
    assert resp.status_code == 201
    run_id = resp.json()["run_id"]
    time.sleep(0.5)
    cancel = client.post(f"/api/research-runs/{run_id}/cancel")
    assert cancel.status_code == 200
    body = _wait_for(client, run_id)
    assert body["status"] == "cancelled"


def test_query_reports_listable_alongside_legacy(query_client):
    client, settings, _ = query_client
    resp = client.post("/api/research-runs", json={"query": "fintech"})
    run_id = resp.json()["run_id"]
    body = _wait_for(client, run_id)
    assert body["status"] == "completed"

    listing = client.get("/api/reports").json()
    mine = [r for r in listing["reports"] if r["id"] == body["report_id"]]
    assert len(mine) == 1
    assert mine[0]["report_type"] == "query"
    assert mine[0]["title"] == "fintech"
    assert mine[0]["article_count"] == 1


def test_query_run_conflict_with_active_run(query_client, monkeypatch):
    client, _, mp = query_client

    async def slow_run(query, settings, cache, **kwargs):
        await asyncio.sleep(30)

    mp.setattr("nodes.runner.execute_query_research", slow_run)
    first = client.post("/api/research-runs", json={"query": "first"})
    assert first.status_code == 201
    time.sleep(0.5)
    second = client.post("/api/research-runs", json={"query": "second"})
    assert second.status_code == 409  # single-active-run policy preserved
    client.post(f"/api/research-runs/{first.json()['run_id']}/cancel")
